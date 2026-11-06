"""The API session: digest login, the MQTT client id, single logins, no secrets in logs."""

import asyncio
import logging
from hashlib import md5
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.petlibro.api import PetLibroAPI
from custom_components.petlibro.exceptions import PetLibroAPIError

BASE = "https://api.us.petlibro.com"
EMAIL = "cat-owner@example.invalid"
DIGEST = md5(b"hunter2").hexdigest()
TOKEN, CLIENT_ID = "TOKEN-SECRET-1", "CLIENTSECRET1"
SECRETS = (TOKEN, CLIENT_ID, DIGEST, EMAIL, "hunter2")


def login_answer(token=TOKEN, client_id=CLIENT_ID):
    return {"code": 0, "data": {"token": token, "clientId": client_id, "memberId": 1, "email": EMAIL,
                                "refreshToken": "REFRESH-SECRET"}}


@pytest.fixture
async def api(hass, aioclient_mock):
    return PetLibroAPI(async_get_clientsession(hass), "America/Los_Angeles", "US", EMAIL, DIGEST)


@pytest.fixture
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.petlibro")
    return caplog


def assert_no_secrets(text):
    for secret in (*SECRETS, "REFRESH-SECRET"):
        assert secret not in text, secret


async def test_re_login_sends_the_digest_and_keeps_the_client_id(api, aioclient_mock, debug_logs):
    aioclient_mock.post(f"{BASE}/member/auth/login", json=login_answer())
    assert await api.session.re_login() == TOKEN
    assert api.session.client_id == CLIENT_ID
    body = aioclient_mock.mock_calls[0][2]
    assert body["password"] == DIGEST and body["email"] == EMAIL
    assert_no_secrets(debug_logs.text)


async def test_config_flow_login_keeps_the_client_id(api, aioclient_mock, debug_logs):
    aioclient_mock.post(f"{BASE}/member/auth/login", json=login_answer())
    assert await api.login(EMAIL, DIGEST) == TOKEN
    assert api.session.client_id == CLIENT_ID
    assert aioclient_mock.mock_calls[0][2]["password"] == DIGEST
    assert_no_secrets(debug_logs.text)


async def test_expired_session_logs_in_again_without_logging_secrets(api, aioclient_mock, debug_logs):
    aioclient_mock.post(f"{BASE}/member/member/info", json={"code": 1009, "msg": "NOT_YET_LOGIN"})
    aioclient_mock.post(f"{BASE}/member/auth/login", json=login_answer())
    api.session.token = "OLD-TOKEN-SECRET"
    await api.session.post("/member/member/info", json={})
    assert api.session.token == TOKEN and api.session.client_id == CLIENT_ID
    assert_no_secrets(debug_logs.text)
    assert "OLD-TOKEN-SECRET" not in debug_logs.text


async def test_answers_are_logged_redacted(api, aioclient_mock, debug_logs):
    aioclient_mock.post(f"{BASE}/device/device/list", json={"code": 0, "data": [{"deviceSn": "SN1", "token": TOKEN}]})
    api.session.token = "T"
    await api.session.post("/device/device/list", json={})
    assert "SN1" in debug_logs.text and TOKEN not in debug_logs.text


async def test_concurrent_callers_share_one_login(api):
    calls = 0

    async def slow_login():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        api.session.client_id = f"client-{calls}"
        return "token"
    api.session.re_login = slow_login
    results = await asyncio.gather(*(api.session.async_ensure_client_id() for _ in range(3)))
    assert calls == 1 and set(results) == {"client-1"}


async def test_refresh_login_only_when_the_id_is_still_the_stale_one(api):
    api.session.client_id = "old"

    async def login():
        api.session.client_id = "new"
        return "token"
    api.session.re_login = AsyncMock(side_effect=login)
    assert await api.session.async_refresh_login("old") == "new"
    assert await api.session.async_refresh_login("old") == "new"  # someone already logged in again
    assert api.session.re_login.await_count == 1


async def test_a_login_without_client_id_is_an_error(api):
    api.session.re_login = AsyncMock(return_value="token")
    with pytest.raises(PetLibroAPIError):
        await api.session.async_ensure_client_id()


async def test_push_endpoints(api, aioclient_mock):
    api.session.token = "T"
    aioclient_mock.post(f"{BASE}/member/app/config", json={"code": 0, "data": {"mqttTlsEnable": True}})
    aioclient_mock.get(f"{BASE}/member/certificate/ca", json={"code": 0, "data": {"caCertificate": "PEM"}})
    aioclient_mock.post(f"{BASE}/member/certificate/generate", json={"code": 0, "data": {"certificate": "C"}})
    assert await api.app_config() == {"mqttTlsEnable": True}
    assert await api.certificate_authority() == {"caCertificate": "PEM"}
    assert await api.generate_certificate("CSR") == {"certificate": "C"}
    assert aioclient_mock.mock_calls[-1][2] == {"csr": "CSR"}
