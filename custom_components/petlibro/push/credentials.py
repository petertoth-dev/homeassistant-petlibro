"""What the live-updates client needs to log in to PETLIBRO's broker.

The app logs in with the client id of the current API login as MQTT client id, username
and password, over TLS with the account's client certificate. A new API login (by this
integration or anywhere else with the same account) ends the MQTT session, so the client
id is always taken from the current login.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import ssl
import tempfile
from typing import Any

from homeassistant.core import HomeAssistant

from ..api import PetLibroAPI
from .certificates import CertificateManager, ClientCertificate
from .const import DEFAULT_BROKER_HOST, DEFAULT_BROKER_TLS_PORT
from .errors import PushCertificateError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerEndpoint:
    """Host and TLS port of the MQTT broker."""

    host: str
    port: int

    @classmethod
    def from_app_config(cls, config: dict[str, Any]) -> BrokerEndpoint:
        """The first TLS broker in /member/app/config, or the known default."""
        for host in config.get("appMqttsHosts") or []:
            if isinstance(host, dict) and isinstance(host.get("host"), str) and isinstance(host.get("port"), int):
                return cls(host=host["host"], port=host["port"])
        return cls(host=DEFAULT_BROKER_HOST, port=DEFAULT_BROKER_TLS_PORT)


@dataclass(frozen=True)
class MqttCredentials:
    """One connection attempt's login."""

    endpoint: BrokerEndpoint
    client_id: str = field(repr=False)
    tls_context: ssl.SSLContext = field(repr=False)


def build_tls_context(certificate: ClientCertificate) -> ssl.SSLContext:
    """TLS context that trusts PETLIBRO's CA and presents the client certificate.

    Blocking (file I/O): run in the executor. `ssl` can only load a key from a file, so
    key and certificate go to a private temporary directory that is removed right after.
    """
    context = ssl.create_default_context(cadata=certificate.ca_pem)
    with tempfile.TemporaryDirectory(prefix="petlibro-") as directory:
        os.chmod(directory, 0o700)
        cert_path = os.path.join(directory, "client.crt")
        key_path = os.path.join(directory, "client.key")
        for path, content in ((cert_path, certificate.certificate_pem), (key_path, certificate.private_key_pem)):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as file:
                file.write(content)
        context.load_cert_chain(cert_path, key_path)
    return context


class PushCredentialProvider:
    """Builds `MqttCredentials` and reacts when the broker rejects them."""

    def __init__(
        self, hass: HomeAssistant, api: PetLibroAPI, certificates: CertificateManager, member_id: int
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._api = api
        self._certificates = certificates
        self._member_id = member_id
        self._endpoint: BrokerEndpoint | None = None

    async def async_get(self) -> MqttCredentials:
        """Credentials for the next connection attempt."""
        client_id = await self._api.session.async_ensure_client_id()
        if self._endpoint is None:
            self._endpoint = BrokerEndpoint.from_app_config(await self._api.app_config())
        certificate = await self._certificates.async_get(self._member_id)
        try:
            context = await self._hass.async_add_executor_job(build_tls_context, certificate)
        except (ssl.SSLError, ValueError) as err:  # unusable key, certificate or CA
            raise PushCertificateError(f"Unusable certificate: {err}") from err
        return MqttCredentials(endpoint=self._endpoint, client_id=client_id, tls_context=context)

    async def async_login_rejected(self, credentials: MqttCredentials) -> None:
        """The broker refused the client id: log in again unless that already happened."""
        _LOGGER.debug("PETLIBRO live updates: login refused, refreshing the API login")
        await self._api.session.async_refresh_login(credentials.client_id)

    def certificate_rejected(self) -> None:
        """TLS failed: issue a new certificate, re-fetch the CA and the broker address."""
        _LOGGER.debug("PETLIBRO live updates: TLS failed, the certificate will be re-issued")
        self._certificates.invalidate()
        self._endpoint = None
