"""Credentials are stored as a digest and secrets never reach the logs."""

from hashlib import md5

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from custom_components.petlibro.security import (
    CONF_PASSWORD_DIGEST,
    AccountCredentials,
    migrate_entry_data,
    password_digest,
    redact,
)

PASSWORD = "correct horse battery staple"
DIGEST = md5(PASSWORD.encode()).hexdigest()


def test_digest_is_what_the_api_logs_in_with():
    assert password_digest(PASSWORD) == DIGEST


def test_credentials_from_a_form_keep_only_the_digest():
    credentials = AccountCredentials.from_user_input("cat@example.invalid", PASSWORD)
    data = credentials.as_entry_data()
    assert data == {CONF_EMAIL: "cat@example.invalid", CONF_PASSWORD_DIGEST: DIGEST}
    assert PASSWORD not in repr(credentials) and DIGEST not in repr(credentials)


@pytest.mark.parametrize("data", [
    {CONF_EMAIL: "cat@example.invalid", CONF_PASSWORD_DIGEST: DIGEST},
    {CONF_EMAIL: "cat@example.invalid", CONF_PASSWORD: PASSWORD},  # entry not migrated yet
])
def test_credentials_from_entry_data(data):
    assert AccountCredentials.from_entry_data(data).password_digest == DIGEST


@pytest.mark.parametrize("data", [{}, {CONF_EMAIL: "cat@example.invalid"}, {CONF_PASSWORD_DIGEST: DIGEST}])
def test_incomplete_entry_data_is_rejected(data):
    with pytest.raises(ValueError):
        AccountCredentials.from_entry_data(data)


def test_migration_replaces_the_password_and_keeps_everything_else():
    migrated = migrate_entry_data({CONF_EMAIL: "e", CONF_PASSWORD: PASSWORD, "region": "US", "api_token": "t"})
    assert migrated == {CONF_EMAIL: "e", CONF_PASSWORD_DIGEST: DIGEST, "region": "US", "api_token": "t"}
    assert migrate_entry_data(migrated) == migrated  # idempotent


def test_redact_hides_secrets_at_any_depth():
    answer = {"code": 0, "data": {"token": "T", "clientId": "C", "memberId": 1,
                                  "pets": [{"name": "Luna", "thumbnailSignedUrl": "https://x"}]}}
    redacted = redact(answer)
    assert redacted["data"]["token"] == redacted["data"]["clientId"] == "**REDACTED**"
    assert redacted["data"]["pets"][0]["thumbnailSignedUrl"] == "**REDACTED**"
    assert redacted["data"]["memberId"] == 1 and redacted["data"]["pets"][0]["name"] == "Luna"
    assert answer["data"]["token"] == "T"  # the original is untouched
    assert redact("plain text") == "plain text"
