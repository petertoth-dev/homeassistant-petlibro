"""The whole integration: setup, live updates, options, unload, removal, migration."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.petlibro.const import DOMAIN, IntegrationSetting
from custom_components.petlibro.security import CONF_PASSWORD_DIGEST

from custom_components.petlibro.exceptions import PetLibroLoginRequired

from .fakes import DUO_SERIAL, MEMBER_ID, POLAR_SERIAL, FakeCloud, Session, push_payload

SESSION = {"email": "tester@example.invalid", "region": "US", "api_token": "t"}
POLAR_TOPIC = f"dl/PLAF109/{POLAR_SERIAL}/app/event/sub"


def make_entry(hass, options=None, data=None, minor_version=3):
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=minor_version, options=options or {},
        data=data or SESSION,
    )
    entry.add_to_hass(hass)
    return entry


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


async def until(hass, condition, timeout=5.0):
    """Wait for the live-update background task (async_block_till_done skips background tasks)."""
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        await hass.async_block_till_done()
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def lid_switch(hass):
    entity_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{POLAR_SERIAL}-manual_feed_now")
    return hass.states.get(entity_id)


async def test_a_push_updates_the_feeder_without_waiting_for_a_poll(hass, cloud, broker):
    """The lid is opened in the app: the cloud pushes, HA re-reads the feeder at once."""
    entry = make_entry(hass)
    with patch("custom_components.petlibro.UPDATE_INTERVAL_SECONDS", 3600):
        hub = await setup(hass, entry)
    await until(hass, broker.open.is_set)  # first connection, nothing pushed yet
    api = cloud["api"]
    assert lid_switch(hass).state == "off" and hub.push.connected

    api.manual_feed_id = 55  # lid opened in the app; the cloud pushes on the next connection
    broker.sessions.append(Session(messages=[(POLAR_TOPIC, push_payload())], stay_open=True))
    broker.open.clear()
    hub.push.client.request_reconnect()
    await until(hass, lambda: broker.open.is_set() and lid_switch(hass).state == "on")

    assert hub.push.connected
    topics = broker.subscriptions[-1]
    assert topics[0] == f"dl/member/{MEMBER_ID}/sub"
    assert f"dl/PLAF205/{DUO_SERIAL}/app/+/sub" in topics and f"dl/PLAF109/{POLAR_SERIAL}/app/+/sub" in topics
    assert broker.credentials[0].client_id == "client0000001"  # the API login's client id
    assert api.certificates.generate_calls == 1  # one certificate, reused on reconnect


async def test_live_updates_can_be_turned_off(hass, cloud, broker):
    hub = await setup(hass, make_entry(hass, options={IntegrationSetting.ENABLE_LIVE_UPDATES: False}))
    assert hub.push is None and broker.credentials == []


async def test_unload_stops_live_updates(hass, cloud, broker):
    entry = make_entry(hass)
    hub = await setup(hass, entry)
    await until(hass, broker.open.is_set)
    push = hub.push
    assert push.connected
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED and not push.connected


async def test_removing_the_entry_deletes_the_certificate(hass, cloud, broker, hass_storage):
    entry = make_entry(hass)
    await setup(hass, entry)
    await until(hass, broker.open.is_set)
    key = f"petlibro.{entry.entry_id}.mqtt_certificate"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_daily_check_reconnects_when_the_certificate_is_due(hass, cloud, broker):
    hub = await setup(hass, make_entry(hass))
    await until(hass, broker.open.is_set)
    broker.open.clear()
    with patch.object(hub.push.certificates, "renewal_due", return_value=True):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
        await until(hass, lambda: len(broker.credentials) == 2 and broker.open.is_set())


@pytest.mark.parametrize(("minor_version", "secret"), [(1, {CONF_PASSWORD: "hunter2"}), (2, {CONF_PASSWORD_DIGEST: "d"})])
async def test_an_old_entry_loses_its_password_or_digest(hass, cloud, broker, minor_version, secret):
    entry = make_entry(hass, minor_version=minor_version, data={**SESSION, **secret})
    await setup(hass, entry)
    assert entry.minor_version == 3 and dict(entry.data) == SESSION
    assert entry.state is ConfigEntryState.LOADED


async def test_an_entry_without_a_session_asks_for_the_password(hass, cloud, broker):
    entry = make_entry(hass, data={"region": "US", "email": "tester@example.invalid"})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert [flow["context"]["source"] for flow in entry.async_get_active_flows(hass, {"reauth"})] == ["reauth"]


async def test_an_ended_session_asks_for_the_password(hass, cloud, broker):
    """PETLIBRO ends the session and no password is stored: re-authentication, no login."""
    entry = make_entry(hass)
    cloud_member_info = FakeCloud.member_info

    async def ended(self):
        self.session.login_required = True
        self.session.on_login_required()
        raise PetLibroLoginRequired("ended")

    with patch.object(FakeCloud, "member_info", ended):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(list(entry.async_get_active_flows(hass, {"reauth"}))) == 1  # started once, not twice
    assert cloud["api"].session.logins == 0
    assert FakeCloud.member_info is cloud_member_info


async def test_a_session_ending_while_running_stops_polling_and_asks_once(hass, cloud, broker):
    entry = make_entry(hass, options={IntegrationSetting.ENABLE_LIVE_UPDATES: False})
    hub = await setup(hass, entry)
    session = cloud["api"].session
    session.login_required = True
    session.on_login_required()
    await hub.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not hub.coordinator.last_update_success
    assert len(list(entry.async_get_active_flows(hass, {"reauth"}))) == 1
