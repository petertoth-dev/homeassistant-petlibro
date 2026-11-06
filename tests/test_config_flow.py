"""Login forms store the password digest, never the password."""

from hashlib import md5
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petlibro.const import DOMAIN
from custom_components.petlibro.security import CONF_PASSWORD_DIGEST

PASSWORD = "hunter2"
DIGEST = md5(PASSWORD.encode()).hexdigest()


def mock_login():
    return patch("custom_components.petlibro.config_flow.PetLibroAPI.login", AsyncMock(return_value="TOKEN"))


def no_setup():
    return patch("custom_components.petlibro.async_setup_entry", AsyncMock(return_value=True))


async def test_user_step_stores_the_digest_only(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with mock_login() as login, no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"region": "US", CONF_EMAIL: "cat@example.invalid", CONF_PASSWORD: PASSWORD}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"region": "US", CONF_EMAIL: "cat@example.invalid",
                              CONF_PASSWORD_DIGEST: DIGEST, "api_token": "TOKEN"}
    assert login.await_args.args == ("cat@example.invalid", DIGEST)  # the API only ever sees the digest
    assert result["minor_version"] == 2


async def test_reauth_replaces_the_digest(hass):
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2, data={
        "region": "US", CONF_EMAIL: "cat@example.invalid", CONF_PASSWORD_DIGEST: "old", "api_token": "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with mock_login(), no_setup():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: PASSWORD})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD_DIGEST] == DIGEST and CONF_PASSWORD not in entry.data
    assert entry.data["api_token"] == "TOKEN"
