"""Nothing password-like is stored, and secrets never reach the logs."""

from hashlib import md5

from homeassistant.const import CONF_API_TOKEN, CONF_EMAIL, CONF_PASSWORD, CONF_REGION

from custom_components.petlibro.security import (
    CONF_CLIENT_ID,
    CONF_PASSWORD_DIGEST,
    AccountCredentials,
    has_session,
    migrate_entry_data,
    password_digest,
    redact,
    session_entry_data,
)

PASSWORD = "correct horse battery staple"
DIGEST = md5(PASSWORD.encode()).hexdigest()


def test_digest_is_what_the_api_logs_in_with():
    assert password_digest(PASSWORD) == DIGEST


def test_typed_password_is_hashed_and_never_shown():
    credentials = AccountCredentials.from_user_input("cat@example.invalid", PASSWORD)
    assert credentials.password_digest == DIGEST
    assert PASSWORD not in repr(credentials) and DIGEST not in repr(credentials)


def test_entry_data_holds_the_session_only():
    data = session_entry_data("US", "cat@example.invalid", "TOKEN", "CLIENT")
    assert data == {CONF_REGION: "US", CONF_EMAIL: "cat@example.invalid", CONF_API_TOKEN: "TOKEN", CONF_CLIENT_ID: "CLIENT"}
    assert has_session(data)


def test_without_token_there_is_no_session():
    assert not has_session({CONF_EMAIL: "cat@example.invalid"})
    assert not has_session({CONF_API_TOKEN: "TOKEN"})


def test_migration_removes_password_and_digest():
    session = {CONF_EMAIL: "e", CONF_REGION: "US", CONF_API_TOKEN: "t"}
    assert migrate_entry_data({**session, CONF_PASSWORD: PASSWORD}) == session  # 1.1
    assert migrate_entry_data({**session, CONF_PASSWORD_DIGEST: DIGEST}) == session  # 1.2
    assert migrate_entry_data(session) == session  # idempotent


def test_redact_hides_secrets_at_any_depth():
    answer = {"code": 0, "data": {"token": "T", "clientId": "C", "memberId": 1,
                                  "pets": [{"name": "Luna", "thumbnailSignedUrl": "https://x"}]}}
    redacted = redact(answer)
    assert redacted["data"]["token"] == redacted["data"]["clientId"] == "**REDACTED**"
    assert redacted["data"]["pets"][0]["thumbnailSignedUrl"] == "**REDACTED**"
    assert redacted["data"]["memberId"] == 1 and redacted["data"]["pets"][0]["name"] == "Luna"
    assert answer["data"]["token"] == "T"  # the original is untouched
    assert redact("plain text") == "plain text"
    assert redact({CONF_CLIENT_ID: "C", CONF_PASSWORD_DIGEST: "D"}) == {CONF_CLIENT_ID: "**REDACTED**", CONF_PASSWORD_DIGEST: "**REDACTED**"}
