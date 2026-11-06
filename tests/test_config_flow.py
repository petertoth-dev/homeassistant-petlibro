"""Login forms store the session only, never the password or its digest."""

from hashlib import md5
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petlibro.const import DOMAIN
from custom_components.petlibro.security import CONF_PASSWORD_DIGEST

SESSION = {"region": "US", CONF_EMAIL: "cat@example.invalid", "api_token": "TOKEN", "client_id": "CLIENT"}

PASSWORD = "hunter2"
DIGEST = md5(PASSWORD.encode()).hexdigest()


def mock_login():
    async def login(api, email, digest):
        api.session.client_id = "CLIENT"
        return "TOKEN"
    return patch("custom_components.petlibro.config_flow.PetLibroAPI.login", autospec=True, side_effect=login)


def no_setup():
    return patch("custom_components.petlibro.async_setup_entry", AsyncMock(return_value=True))


async def test_user_step_stores_the_session_only(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with mock_login() as login, no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"region": "US", CONF_EMAIL: "cat@example.invalid", CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == SESSION
    assert login.await_args.args[1:] == ("cat@example.invalid", DIGEST)  # the API only ever sees the digest
    assert result["minor_version"] == 3


async def test_reauth_stores_the_new_session_only(hass):
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=3, data={
        "region": "US", CONF_EMAIL: "cat@example.invalid", "api_token": "old", "client_id": "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with mock_login(), no_setup():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert dict(entry.data) == SESSION
    assert CONF_PASSWORD_DIGEST not in entry.data and CONF_PASSWORD not in entry.data
