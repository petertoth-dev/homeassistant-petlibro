"""Credential handling and redaction for the PETLIBRO integration.

Nothing password-like is ever stored. A login form hashes the typed password (the API
logs in with its MD5 digest), makes one login and drops both. Home Assistant keeps only
that login's session: the API token and the MQTT client id. When PETLIBRO ends the
session, the integration asks for the password again (Home Assistant's re-authentication).
The token and the client id are still credentials for this account, so they, like every
other secret, are redacted from every log line and any diagnostics.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import md5
from typing import Any

from homeassistant.const import CONF_API_TOKEN, CONF_EMAIL, CONF_PASSWORD, CONF_REGION
from homeassistant.helpers.redact import async_redact_data

# Entry key of the login's MQTT client id (client id = username = password on the broker).
CONF_CLIENT_ID = "client_id"
# Entry key used by config entry 1.2 for the password digest; only read to remove it.
CONF_PASSWORD_DIGEST = "password_md5"

# Keys whose values are secrets or personal data in API answers, MQTT payloads and entry data.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "accessToken",
    "api_token",
    "caCertificate",
    "cameraAuthInfo",
    "certificate",
    "clientId",
    CONF_CLIENT_ID,
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
    """A login typed into a form: e-mail plus the password digest. Kept in memory only."""

    email: str
    password_digest: str = field(repr=False)

    @classmethod
    def from_user_input(cls, email: str, password: str) -> AccountCredentials:
        """Credentials from a login form; the typed password is hashed at once."""
        return cls(email=email, password_digest=password_digest(password))


def session_entry_data(region: str, email: str, token: str, client_id: str | None) -> dict[str, Any]:
    """Entry data after a login: the account and its session, never a password or digest."""
    return {CONF_REGION: region, CONF_EMAIL: email, CONF_API_TOKEN: token, CONF_CLIENT_ID: client_id}


def has_session(data: Mapping[str, Any]) -> bool:
    """True when entry data holds an account and a session token to start with."""
    return bool(data.get(CONF_EMAIL) and data.get(CONF_API_TOKEN))


def migrate_entry_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Remove a stored password (1.1) or password digest (1.2) from entry data (→ 1.3)."""
    return {key: value for key, value in data.items() if key not in (CONF_PASSWORD, CONF_PASSWORD_DIGEST)}
