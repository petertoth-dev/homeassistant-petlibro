"""Credential handling and redaction for the PETLIBRO integration.

The PETLIBRO API never needs the account password itself: every login sends the MD5
digest of it. The integration therefore stores only that digest, so the password the
user typed (which may be reused elsewhere) is never written to disk. The digest, the
session token and the MQTT login are still credentials for this account, so they are
redacted from every log line and any diagnostics.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import md5
from typing import Any

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.redact import async_redact_data

# Config entry key holding the MD5 digest the API logs in with (replaces the password).
CONF_PASSWORD_DIGEST = "password_md5"

# Keys whose values are secrets or personal data in API answers, MQTT payloads and entry data.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "accessToken",
    "api_token",
    "caCertificate",
    "cameraAuthInfo",
    "certificate",
    "clientId",
    "csr",
    "email",
    "password",
    CONF_PASSWORD_DIGEST,
    "pem",
    "phone",
    "privateKey",
    "private_key_pem",
    "pushId",
    "refreshToken",
    "thumbnailSignedUrl",
    "token",
    "userToken",
})


def password_digest(password: str) -> str:
    """Return the value the PETLIBRO API expects in place of the password."""
    return md5(password.encode("UTF-8")).hexdigest()


def redact(data: Any) -> Any:
    """Return a copy of an API/MQTT structure with every sensitive value replaced."""
    if isinstance(data, (Mapping, list)):
        return async_redact_data(data, SENSITIVE_KEYS)
    return data


@dataclass(frozen=True)
class AccountCredentials:
    """The account login the integration keeps: e-mail plus the password digest."""

    email: str
    password_digest: str = field(repr=False)

    @classmethod
    def from_entry_data(cls, data: Mapping[str, Any]) -> AccountCredentials:
        """Read credentials from config entry data (digest, or a not yet migrated password)."""
        email = data.get(CONF_EMAIL)
        digest = data.get(CONF_PASSWORD_DIGEST)
        if not digest and (password := data.get(CONF_PASSWORD)):
            digest = password_digest(password)
        if not email or not digest:
            raise ValueError("Config entry has no PETLIBRO e-mail or password")
        return cls(email=email, password_digest=digest)

    @classmethod
    def from_user_input(cls, email: str, password: str) -> AccountCredentials:
        """Credentials from a login form; the typed password is hashed at once."""
        return cls(email=email, password_digest=password_digest(password))

    def as_entry_data(self) -> dict[str, str]:
        """Entry data to store: the e-mail and the digest, never the password."""
        return {CONF_EMAIL: self.email, CONF_PASSWORD_DIGEST: self.password_digest}


def migrate_entry_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Replace a stored plain-text password with its digest (config entry 1.1 → 1.2)."""
    migrated = {key: value for key, value in data.items() if key != CONF_PASSWORD}
    if password := data.get(CONF_PASSWORD):
        migrated[CONF_PASSWORD_DIGEST] = password_digest(password)
    return migrated
