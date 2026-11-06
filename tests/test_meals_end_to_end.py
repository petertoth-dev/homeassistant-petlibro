"""Meals per cat through the whole integration, and the Polar lid following the app."""

import asyncio
import json
import logging
from datetime import timedelta
from hashlib import md5

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache_with_extra_data

from custom_components.petlibro.const import DOMAIN, IntegrationSetting
from custom_components.petlibro.security import CONF_PASSWORD_DIGEST

from .fakes import DUO_SERIAL, LUNA, MILO, POLAR_SERIAL, meal_record, ms

DUO_MAC = "AABBCCDDEE01"
NO_PUSH = {IntegrationSetting.ENABLE_LIVE_UPDATES: False}  # these tests drive the polls themselves


def entry(hass):
    config_entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2, options=NO_PUSH, data={
        "email": "tester@example.invalid", CONF_PASSWORD_DIGEST: md5(b"x").hexdigest(), "region": "US", "api_token": "t"})
    config_entry.add_to_hass(hass)
    return config_entry


def entity_id(hass, platform, unique_id):
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found, f"no {platform} {unique_id}"
    return found


async def start(hass, config_entry):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][config_entry.entry_id]


async def poll(hass, hub):
    """One poll, past the integration's 10 s per-object throttle."""
    hub.last_refresh_times.clear()
    hub.api._cached_responses = {}
    await hub.coordinator.async_refresh()
    await hass.async_block_till_done()


def three_meals():
    start_time = dt_util.now() - timedelta(hours=2)
    return [
        meal_record(1, start_time, 66, 12.0, [{"petId": LUNA, "intake": 12.0, "duration": 60}]),
        meal_record(2, start_time + timedelta(minutes=30), 200, 20.0,
                    [{"petId": LUNA, "intake": 5.0, "duration": 80}, {"petId": MILO, "intake": 15.0, "duration": 120}]),
        meal_record(3, start_time + timedelta(minutes=60), 30, 4.5, [{"petId": -1, "intake": 4.5}]),
    ]


async def test_meals_per_cat_and_a_new_meal(hass, cloud, caplog, freezer):
    freezer.move_to("2026-11-05 12:00:00-08:00")
    caplog.set_level(logging.WARNING)
    hub = await start(hass, entry(hass))
    api = cloud["api"]
    api.meals = three_meals()
    await poll(hass, hub)

    eaten = hass.states.get(entity_id(hass, "sensor", f"{DUO_SERIAL}-today_consumed_food_weight-{DUO_MAC}"))
    assert eaten.state == "36.5" and [m["grams"] for m in eaten.attributes["meals"]] == [12.0, 20.0, 4.5]
    assert hass.states.get(entity_id(hass, "sensor", f"{DOMAIN}-today_feeder_intake-{LUNA}")).state == "17.0"
    assert hass.states.get(entity_id(hass, "sensor", f"{DOMAIN}-today_feeder_intake-{MILO}")).state == "15.0"
    meal_event = entity_id(hass, "event", f"{DUO_SERIAL}-meal")
    assert hass.states.get(meal_event).state == "unknown"  # existing meals do not fire on install

    api.meals.append(meal_record(4, dt_util.now() - timedelta(minutes=3), 90, 6.0,
                                 [{"petId": MILO, "intake": 6.0, "duration": 90}]))
    api.messages.insert(0, {"id": 502, "eventKey": "FOOD_LOW", "timestamp": ms(dt_util.now()),
                            "param": json.dumps({"deviceSn": DUO_SERIAL})})
    await poll(hass, hub)
    fired = hass.states.get(meal_event)
    assert fired.attributes["pet_names"] == ["Milo"] and fired.attributes["grams"] == 6.0
    assert hass.states.get(entity_id(hass, "event", f"{DUO_SERIAL}-notification")).attributes["event_key"] == "FOOD_LOW"
    assert hass.states.get(entity_id(hass, "sensor", f"{DOMAIN}-today_feeder_intake-{MILO}")).state == "21.0"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and "petlibro" in r.name]


async def test_meals_eaten_while_down_fire_once_after_restart(hass, cloud, freezer):
    freezer.move_to("2026-11-05 12:00:00-08:00")
    meals = three_meals()
    last_fired = dt_util.utc_from_timestamp(meals[0]["recordTime"] / 1000).isoformat()
    mock_restore_cache_with_extra_data(hass, [(
        State("event.duo_meal", "2026-11-05T18:01:06.000+00:00", {"event_type": "meal", "record_time": last_fired}),
        {"last_event_type": "meal", "last_event_attributes": {"record_id": 1, "record_time": last_fired}},
    )])
    hub = await start(hass, entry(hass))
    cloud["api"].meals = meals
    fired = []
    hass.bus.async_listen("state_changed", lambda event: fired.append(event.data["new_state"].attributes.get("record_id"))
                          if event.data["entity_id"] == "event.duo_meal" else None)
    for _ in range(2):
        await poll(hass, hub)
    assert [record_id for record_id in fired if record_id is not None] == [2, 3]


async def test_the_lid_switch_follows_the_app(hass, cloud):
    """Opened or closed in the app, the lid switch follows on the next refresh."""
    hub = await start(hass, entry(hass))
    lid = entity_id(hass, "switch", f"{POLAR_SERIAL}-manual_feed_now")
    api = cloud["api"]
    api.manual_feed_id = 77  # opened in the app
    await poll(hass, hub)
    assert hass.states.get(lid).state == "on"
    api.manual_feed_id = None  # closed in the app (this stayed "on" before the fix)
    await poll(hass, hub)
    assert hass.states.get(lid).state == "off"
