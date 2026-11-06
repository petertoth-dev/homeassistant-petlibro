"""What happens when a live update arrives."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.util.dt import utcnow

from ..exceptions import PetLibroAPIError
from ..security import redact
from .const import REFRESH_COOLDOWN_SECONDS
from .messages import PushMessage

if TYPE_CHECKING:
    from ..hub import PetLibroHub

_LOGGER = logging.getLogger(__name__)

MessageHandler = Callable[[PushMessage], Awaitable[None]]


class PushDispatcher:
    """Routes messages to the handlers registered for their command."""

    def __init__(self) -> None:
        """Initialize with no handlers."""
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._reported: set[str | None] = set()

    def register(self, command: str, handler: MessageHandler) -> Callable[[], None]:
        """Handle `command` with `handler`; returns a function that unregisters it."""
        handlers = self._handlers.setdefault(command, [])
        handlers.append(handler)
        return lambda: handlers.remove(handler)

    async def async_dispatch(self, message: PushMessage) -> None:
        """Run every handler for the message's command."""
        handlers = self._handlers.get(message.command or "")
        if not handlers:
            self._report_unhandled(message)
            return
        _LOGGER.debug("PETLIBRO live update %s for %s, event keys %s",
                      message.command, message.serial or "the account", message.event_keys)
        for handler in list(handlers):
            await handler(message)

    def _report_unhandled(self, message: PushMessage) -> None:
        # Unknown commands are logged once (at info), so new message types can be discovered.
        level = logging.DEBUG if message.command in self._reported else logging.INFO
        self._reported.add(message.command)
        _LOGGER.log(level, "PETLIBRO live update not handled yet: %s on %s (%s): %s",
                    message.command, message.topic.scope, message.topic.channel, redact(message.payload))


class DeviceRefresher:
    """Re-reads a feeder from the cloud right after it reports a change.

    Each device gets a debouncer: the first push refreshes at once, more pushes within the
    cooldown become one more refresh at its end.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, hub: PetLibroHub,
                 cooldown: float = REFRESH_COOLDOWN_SECONDS) -> None:
        """Initialize."""
        self._hass = hass
        self._entry = entry
        self._hub = hub
        self._cooldown = cooldown
        self._debouncers: dict[str, Debouncer] = {}

    async def async_handle(self, message: PushMessage) -> None:
        """`MessageHandler` for refresh commands about a device."""
        if message.serial:
            self.request(message.serial)

    @callback
    def request(self, serial: str) -> None:
        """Schedule a refresh of one device (ignored for unknown serials)."""
        if serial not in self._hub.devices:
            _LOGGER.debug("PETLIBRO live update for an unknown device ignored")
            return
        if (debouncer := self._debouncers.get(serial)) is None:
            debouncer = self._debouncers[serial] = Debouncer(
                self._hass, _LOGGER, cooldown=self._cooldown, immediate=True,
                function=partial(self._async_refresh, serial),
            )
        self._entry.async_create_background_task(
            self._hass, debouncer.async_call(), f"petlibro live refresh {serial[-4:]}"
        )

    async def _async_refresh(self, serial: str) -> None:
        if (device := self._hub.devices.get(serial)) is None:
            return
        try:
            await device.refresh()
        except PetLibroAPIError as err:
            _LOGGER.warning("PETLIBRO live update: refreshing %s failed: %s", device.name, err)
            return
        self._hub.last_refresh_times[serial] = utcnow()
        self._hub.coordinator.async_update_listeners()

    @callback
    def async_shutdown(self) -> None:
        """Cancel pending refreshes."""
        for debouncer in self._debouncers.values():
            debouncer.async_shutdown()
        self._debouncers.clear()
