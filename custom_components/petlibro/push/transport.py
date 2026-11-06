"""MQTT transport for live updates: an interface plus the aiomqtt implementation.

The client depends only on `MqttTransport` / `MqttConnection`, so tests can run it against
a fake broker and the library can change without touching the reconnect logic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
import logging
import ssl
from typing import Protocol

import aiomqtt

from .const import KEEPALIVE_SECONDS
from .credentials import MqttCredentials
from .errors import PushCertificateError, PushConnectionError, PushError, PushLoginRefused

__all__ = ["AiomqttTransport", "MqttConnection", "MqttTransport", "PushCertificateError",
           "PushConnectionError", "PushError", "PushLoginRefused", "translate_error"]

_LOGGER = logging.getLogger(__name__)

# paho logs "Sending CONNECT ... client_id=<id>" at DEBUG, and the client id is this
# login's MQTT password. Its logger is pinned to INFO so that line is never written,
# even when debug logging is enabled for the integration.
_PAHO_LOGGER = _LOGGER.getChild("paho")
_PAHO_LOGGER.setLevel(logging.INFO)

# CONNACK refusals that mean "this login is not accepted": MQTT 3.1.1 codes 4/5 and the
# reason codes paho reports for them (134 bad user name or password, 135 not authorized).
LOGIN_REFUSED_CODES = frozenset({4, 5, 134, 135})
SUBSCRIPTION_REFUSED = 0x80


class MqttConnection(Protocol):
    """An open broker connection."""

    async def subscribe(self, topics: Sequence[str]) -> None:
        """Subscribe to the topics."""

    def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        """Incoming (topic, payload) pairs until the connection ends."""


class MqttTransport(Protocol):
    """Opens broker connections."""

    def connect(self, credentials: MqttCredentials) -> AbstractAsyncContextManager[MqttConnection]:
        """Connect with the credentials; closing the context disconnects."""


def _code_value(code: object) -> int | None:
    value = getattr(code, "value", code)
    return value if isinstance(value, int) else None


def translate_error(error: aiomqtt.MqttError) -> PushError:
    """Map an aiomqtt error to what the client should do about it."""
    if isinstance(error, aiomqtt.MqttCodeError) and _code_value(error.rc) in LOGIN_REFUSED_CODES:
        return PushLoginRefused(str(error))
    if isinstance(error.__cause__ or error.__context__, ssl.SSLError):
        return PushCertificateError(str(error))
    return PushConnectionError(str(error))


class _AiomqttConnection:
    """`MqttConnection` over an aiomqtt client."""

    def __init__(self, client: aiomqtt.Client) -> None:
        self._client = client

    async def subscribe(self, topics: Sequence[str]) -> None:
        granted = await self._client.subscribe([(topic, 0) for topic in topics])
        for topic, code in zip(topics, granted, strict=False):
            if (_code_value(code) or 0) >= SUBSCRIPTION_REFUSED:
                _LOGGER.warning("PETLIBRO live updates: subscription refused for %s", topic)

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        try:
            async for message in self._client.messages:
                payload = message.payload
                yield message.topic.value, payload if isinstance(payload, bytes) else str(payload).encode()
        except aiomqtt.MqttError as err:
            raise translate_error(err) from err


class AiomqttTransport:
    """`MqttTransport` using aiomqtt (MQTT 3.1.1 over TLS)."""

    @asynccontextmanager
    async def connect(self, credentials: MqttCredentials) -> AsyncIterator[MqttConnection]:
        client = aiomqtt.Client(
            hostname=credentials.endpoint.host,
            port=credentials.endpoint.port,
            identifier=credentials.client_id,
            username=credentials.client_id,
            password=credentials.client_id,
            tls_context=credentials.tls_context,
            keepalive=KEEPALIVE_SECONDS,
            clean_session=True,
            logger=_PAHO_LOGGER,
        )
        try:
            async with client:
                yield _AiomqttConnection(client)
        except aiomqtt.MqttError as err:
            raise translate_error(err) from err
