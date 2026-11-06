"""MQTT client certificates for PETLIBRO live updates: issue, store, renew.

PETLIBRO's broker needs TLS with a client certificate the cloud signs for the account
(`/member/certificate/generate`, CSR subject SERIALNUMBER=<memberId>, CN=DesignLibro) and is
verified with PETLIBRO's own CA (`/member/certificate/ca`). Both expire, so they are renewed
ahead of time and re-issued when the broker rejects them. The private key never leaves
Home Assistant: it is created here, kept in a private store (file mode 0600) and only the
CSR is sent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import logging
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..api import PetLibroAPI
from ..const import DOMAIN
from ..exceptions import PetLibroAPIError
from .const import CERTIFICATE_RENEW_BEFORE, CSR_COMMON_NAME, RSA_KEY_SIZE

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
PEM_BEGIN = "-----BEGIN"


def normalize_pem(text: str) -> str:
    """Drop anything before the PEM block (PETLIBRO's CA comes with a text header)."""
    if PEM_BEGIN not in text:
        raise ValueError("No PEM block")
    return text[text.index(PEM_BEGIN):].strip() + "\n"


def certificate_expiry(pem: str) -> datetime:
    """The certificate's own end of validity (UTC)."""
    return x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc


def build_key_and_csr(member_id: int) -> tuple[str, str]:
    """Create an RSA key and the CSR PETLIBRO signs. CPU-bound: run it in the executor."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
    subject = x509.Name([
        x509.NameAttribute(NameOID.SERIAL_NUMBER, str(member_id)),
        x509.NameAttribute(NameOID.COMMON_NAME, CSR_COMMON_NAME),
    ])
    csr = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(key, hashes.SHA256())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key_pem, csr.public_bytes(serialization.Encoding.PEM).decode()


@dataclass(frozen=True)
class ClientCertificate:
    """Everything needed for the TLS side of the broker connection."""

    member_id: int
    serial_number: str
    private_key_pem: str = field(repr=False)
    certificate_pem: str = field(repr=False)
    ca_pem: str = field(repr=False)

    @property
    def certificate_expires(self) -> datetime:
        """When the client certificate stops being valid."""
        return certificate_expiry(self.certificate_pem)

    @property
    def ca_expires(self) -> datetime:
        """When PETLIBRO's CA certificate stops being valid."""
        return certificate_expiry(self.ca_pem)

    def certificate_due(self, now: datetime) -> bool:
        """True when the client certificate is within the renewal window."""
        return now + CERTIFICATE_RENEW_BEFORE >= self.certificate_expires

    def ca_due(self, now: datetime) -> bool:
        """True when the CA certificate is within the renewal window."""
        return now + CERTIFICATE_RENEW_BEFORE >= self.ca_expires

    def as_dict(self) -> dict[str, Any]:
        """Serializable form for the store."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClientCertificate:
        """Rebuild from the store; raises on missing or invalid data."""
        certificate = cls(**{name: data[name] for name in cls.__dataclass_fields__})
        certificate.certificate_expires  # noqa: B018 - validates both PEMs
        certificate.ca_expires  # noqa: B018
        return certificate


class CertificateStore:
    """Private, atomically written storage of one config entry's certificate."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store for one config entry."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.mqtt_certificate", private=True, atomic_writes=True
        )

    async def async_load(self) -> ClientCertificate | None:
        """Load the stored certificate, or None when there is none or it is unreadable."""
        data = await self._store.async_load()
        if not data:
            return None
        try:
            return ClientCertificate.from_dict(data)
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning("Stored PETLIBRO live-update certificate is unreadable; a new one will be issued")
            return None

    async def async_save(self, certificate: ClientCertificate) -> None:
        """Store the certificate."""
        await self._store.async_save(certificate.as_dict())

    async def async_remove(self) -> None:
        """Delete the stored certificate (when the config entry is removed)."""
        await self._store.async_remove()


class CertificateManager:
    """Hands out a valid certificate: reuses the stored one, renews what is due."""

    def __init__(self, hass: HomeAssistant, api: PetLibroAPI, store: CertificateStore) -> None:
        """Initialize."""
        self._hass = hass
        self._api = api
        self._store = store
        self._current: ClientCertificate | None = None
        self._loaded = False
        self._reissue = False

    def invalidate(self) -> None:
        """The broker rejected the certificate: issue a new one and re-fetch the CA next time."""
        self._reissue = True

    def renewal_due(self, now: datetime | None = None) -> bool:
        """True when the certificate in use (if any) should be renewed now."""
        now = now or dt_util.utcnow()
        current = self._current
        return self._reissue or (current is not None and (current.certificate_due(now) or current.ca_due(now)))

    async def async_get(self, member_id: int) -> ClientCertificate:
        """Return a certificate valid for `member_id`, issuing or renewing parts as needed."""
        if not self._loaded:
            self._current = await self._store.async_load()
            self._loaded = True
        now = dt_util.utcnow()
        current = self._current
        reissue = self._reissue or current is None or current.member_id != member_id or current.certificate_due(now)
        refresh_ca = reissue or current.ca_due(now)
        if not reissue and not refresh_ca:
            return current

        ca_pem = await self._async_fetch_ca() if refresh_ca else current.ca_pem
        if reissue:
            certificate = await self._async_issue(member_id, ca_pem)
            _LOGGER.info("PETLIBRO live updates: new client certificate, valid until %s",
                         certificate.certificate_expires.date())
        else:
            certificate = ClientCertificate(
                member_id=current.member_id,
                serial_number=current.serial_number,
                private_key_pem=current.private_key_pem,
                certificate_pem=current.certificate_pem,
                ca_pem=ca_pem,
            )
            _LOGGER.info("PETLIBRO live updates: CA certificate refreshed, valid until %s",
                         certificate.ca_expires.date())
        await self._store.async_save(certificate)
        self._current = certificate
        self._reissue = False
        return certificate

    async def _async_fetch_ca(self) -> str:
        answer = await self._api.certificate_authority()
        pem = answer.get("caCertificate")
        if not isinstance(pem, str):
            raise PetLibroAPIError("No CA certificate in the answer")
        try:
            pem = normalize_pem(pem)
            certificate_expiry(pem)
        except ValueError as err:
            raise PetLibroAPIError(f"Invalid CA certificate: {err}") from err
        return pem

    async def _async_issue(self, member_id: int, ca_pem: str) -> ClientCertificate:
        key_pem, csr_pem = await self._hass.async_add_executor_job(build_key_and_csr, member_id)
        answer = await self._api.generate_certificate(csr_pem)
        certificate_pem = answer.get("certificate")
        if not isinstance(certificate_pem, str):
            raise PetLibroAPIError("No certificate in the answer")
        try:
            certificate_pem = normalize_pem(certificate_pem)
            certificate_expiry(certificate_pem)
        except ValueError as err:
            raise PetLibroAPIError(f"Invalid client certificate: {err}") from err
        return ClientCertificate(
            member_id=member_id,
            serial_number=str(answer.get("serialNumber") or ""),
            private_key_pem=key_pem,
            certificate_pem=certificate_pem,
            ca_pem=ca_pem,
        )
