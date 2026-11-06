"""Live updates from PETLIBRO's cloud (the push channel the PETLIBRO app uses).

Instead of waiting for the next poll, the integration listens on the app's MQTT channel
and re-reads a device as soon as the cloud says it changed (``APP_REFRESH_EVENT``). See
docs/api-documentation.md, "Live updates".

Parts:
- `certificates` — TLS client certificate + CA: issue, store privately, renew.
- `credentials`  — broker address, MQTT login (the API login's client id), TLS context.
- `transport`    — MQTT connection behind an interface (aiomqtt implementation).
- `client`       — connect / subscribe / reconnect loop.
- `messages`     — topics and payload parsing.
- `dispatcher`   — command → handler; device refresh with debouncing.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .certificates import CertificateManager, CertificateStore
from .client import PushClient
from .const import CERTIFICATE_CHECK_INTERVAL, CMD_APP_REFRESH
from .credentials import PushCredentialProvider
from .dispatcher import DeviceRefresher, PushDispatcher
from .messages import device_topic, member_topic
from .transport import AiomqttTransport, MqttTransport

if TYPE_CHECKING:
    from ..hub import PetLibroHub

_LOGGER = logging.getLogger(__name__)

__all__ = ["PetLibroPush"]


class PetLibroPush:
    """Live updates for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        hub: PetLibroHub,
        member_id: int,
        transport: MqttTransport | None = None,
    ) -> None:
        """Wire the parts together; nothing connects before `async_start`."""
        self._hass = hass
        self._entry = entry
        self._hub = hub
        self._member_id = member_id
        self.certificates = CertificateManager(hass, hub.api, CertificateStore(hass, entry.entry_id))
        self.refresher = DeviceRefresher(hass, entry, hub)
        self.dispatcher = PushDispatcher()
        self.dispatcher.register(CMD_APP_REFRESH, self.refresher.async_handle)
        self.client = PushClient(
            transport or AiomqttTransport(),
            PushCredentialProvider(hass, hub.api, self.certificates, member_id),
            self.topics,
            self.dispatcher.async_dispatch,
        )

    @property
    def connected(self) -> bool:
        """True while live updates are flowing."""
        return self.client.connected

    def topics(self) -> list[str]:
        """The account topic plus one topic per known device."""
        topics = [member_topic(self._member_id)]
        topics.extend(
            device_topic(device.model, device.serial)
            for device in self._hub.devices.values()
            if device.model and device.serial
        )
        return topics

    @callback
    def async_start(self) -> None:
        """Start the connection loop and the daily certificate check (both end on unload)."""
        self._entry.async_create_background_task(self._hass, self.client.async_run(), "petlibro live updates")
        self._entry.async_on_unload(
            async_track_time_interval(self._hass, self._async_check_certificates, CERTIFICATE_CHECK_INTERVAL)
        )
        self._entry.async_on_unload(self.refresher.async_shutdown)

    async def _async_check_certificates(self, now: datetime) -> None:
        """Daily: reconnect when the certificate or CA is due, so the loop renews them."""
        if self.certificates.renewal_due(now):
            _LOGGER.info("PETLIBRO live updates: certificate renewal due, reconnecting")
            self.client.request_reconnect()
