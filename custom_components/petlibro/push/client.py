"""The live-updates connection: connect, subscribe, hand messages on, reconnect."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import contextlib
import logging

from ..exceptions import PetLibroAPIError
from .backoff import ExponentialBackoff
from .credentials import MqttCredentials, PushCredentialProvider
from .messages import PushMessage, parse_message
from .errors import PushCertificateError, PushConnectionError, PushLoginRefused
from .transport import MqttConnection, MqttTransport

_LOGGER = logging.getLogger(__name__)

MessageCallback = Callable[[PushMessage], Awaitable[None]]
ConnectedCallback = Callable[[bool], None]


class PushClient:
    """Keeps one broker connection alive for as long as `async_run` runs.

    - A refused login refreshes the API login (the client id changed).
    - A TLS failure re-issues the certificate and re-fetches the CA.
    - Any other failure just waits; waits double up to five minutes and reset once connected.
    - `request_reconnect()` drops the connection on purpose (e.g. after a certificate renewal).
    """

    def __init__(
        self,
        transport: MqttTransport,
        credentials: PushCredentialProvider,
        topics: Callable[[], Sequence[str]],
        on_message: MessageCallback,
        backoff: ExponentialBackoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Initialize."""
        self._transport = transport
        self._credentials = credentials
        self._topics = topics
        self._on_message = on_message
        self._backoff = backoff or ExponentialBackoff()
        self._sleep = sleep
        self._reconnect = asyncio.Event()
        self._connected = False
        self._connected_listeners: list[ConnectedCallback] = []

    @property
    def connected(self) -> bool:
        """True while subscribed to the broker."""
        return self._connected

    def add_connected_listener(self, listener: ConnectedCallback) -> Callable[[], None]:
        """Call `listener(connected)` on every change; returns a function that removes it."""
        self._connected_listeners.append(listener)
        return lambda: self._connected_listeners.remove(listener)

    def request_reconnect(self) -> None:
        """Drop the current connection; the loop connects again with fresh credentials."""
        self._reconnect.set()

    async def async_run(self) -> None:
        """Run until cancelled."""
        while True:
            await self._async_attempt()
            delay = self._backoff.next_delay()
            _LOGGER.debug("PETLIBRO live updates: reconnecting in %.0f s", delay)
            await self._sleep(delay)

    async def _async_attempt(self) -> None:
        credentials: MqttCredentials | None = None
        self._reconnect.clear()
        try:
            credentials = await self._credentials.async_get()
            async with self._transport.connect(credentials) as connection:
                topics = list(self._topics())
                await connection.subscribe(topics)
                _LOGGER.debug("PETLIBRO live updates: subscribed to %d topics", len(topics))
                self._backoff.reset()
                self._set_connected(True)
                await self._async_listen(connection)
        except PushLoginRefused:
            _LOGGER.info("PETLIBRO live updates: login refused, logging in again")
            if credentials is not None:
                with contextlib.suppress(PetLibroAPIError):
                    await self._credentials.async_login_rejected(credentials)
        except PushCertificateError as err:
            _LOGGER.warning("PETLIBRO live updates: TLS failed (%s); renewing the certificate", err)
            self._credentials.certificate_rejected()
        except (PushConnectionError, PetLibroAPIError, OSError) as err:
            _LOGGER.info("PETLIBRO live updates: connection failed: %s", err)
        finally:
            self._set_connected(False)

    async def _async_listen(self, connection: MqttConnection) -> None:
        """Dispatch messages until the connection ends or a reconnect is requested."""
        listen = asyncio.ensure_future(self._async_dispatch_all(connection))
        reconnect = asyncio.ensure_future(self._reconnect.wait())
        try:
            done, _ = await asyncio.wait({listen, reconnect}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (listen, reconnect):
                task.cancel()
            await asyncio.gather(listen, reconnect, return_exceptions=True)
        if listen in done:
            listen.result()  # re-raises a connection error
            _LOGGER.info("PETLIBRO live updates: the broker closed the connection")
        else:
            _LOGGER.debug("PETLIBRO live updates: reconnect requested")

    async def _async_dispatch_all(self, connection: MqttConnection) -> None:
        async for topic, payload in connection.messages():
            message = parse_message(topic, payload)
            if message is None:
                _LOGGER.debug("PETLIBRO live updates: ignored message on %s", topic)
                continue
            try:
                await self._on_message(message)
            except Exception:  # a broken handler must not end the connection
                _LOGGER.exception("PETLIBRO live updates: handling %s failed", message.command)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        _LOGGER.info("PETLIBRO live updates %s", "connected" if connected else "disconnected")
        for listener in list(self._connected_listeners):
            listener(connected)
