"""Granary 2 Vision meals, per-pet totals and event entities; synthetic responses, no cloud."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from custom_components.petlibro.api import PetLibroAPI
from custom_components.petlibro.const import PLATFORMS, Unit
from custom_components.petlibro.devices import product_name_map
from custom_components.petlibro.devices.feeders.granary_2_vision_feeder import Granary2VisionFeeder
from custom_components.petlibro.event import DEVICE_EVENT_MAP, PetLibroEventEntity
from custom_components.petlibro.exceptions import PetLibroAPIError
from custom_components.petlibro.pets import Pet
from custom_components.petlibro.pets.entity import PET_ENTITY_MAP, PL_PetSensorEntity
from custom_components.petlibro.sensor import DEVICE_SENSOR_MAP, PetLibroSensorEntity

from .fakes import meal_record, ms

TZ = "America/Los_Angeles"
HORUSZ, MARGIE = 1001, 1002


def today_at(hour: int, minute: int, second: int = 0) -> datetime:
    return dt_util.start_of_local_day() + timedelta(hours=hour, minutes=minute, seconds=second)


@pytest.fixture(autouse=True)
def noon(freezer):
    """Pin the clock to midday in the harness's US/Pacific zone, so 'today' has room on both sides."""
    freezer.move_to("2026-11-05 12:00:00-08:00")


def make_hub(hass):
    api = Mock(spec=PetLibroAPI)
    for method in (
        "device_base_info", "device_attribute_settings", "get_device_upgrade",
        "device_feeding_plan_today_new", "device_grain_status", "device_events",
        "tutk_info", "device_tutk_info", "device_get_free_feeding_setting",
    ):
        getattr(api, method).return_value = {}
    api.device_real_info.return_value = {"online": True}
    api.device_get_bound_pets.return_value = []
    api.get_device_work_record.return_value = []
    api.device_feeding_plan_list.return_value = []
    api.device_data_real_info.return_value = {"bowlMode": "SINGLE_BOWL", "remainingGrain": 16}
    api.get_device_meal_records.return_value = []
    api.device_messages.return_value = []
    hub = SimpleNamespace(
        api=api,
        member=SimpleNamespace(feedUnitType=Unit.CUPS, id=1),
        coordinator=DataUpdateCoordinator(hass, logging.getLogger(__name__), name="test", config_entry=None),
        pets={},
        devices={},
        pets_helper=SimpleNamespace(),
        unit_entities=SimpleNamespace(unique_ids={}),
        entry=SimpleNamespace(options={}),
    )
    hub.coordinator.last_update_success = True
    return hub


def make_feeder(hub, serial="vision-1", name="Kitchen Vision"):
    feeder = Granary2VisionFeeder({
        "deviceSn": serial, "productName": "Granary 2 Vision", "name": name, "device_id": f"ha-{serial}",
    }, hub)
    feeder.saved_to_options = True
    hub.devices[serial] = feeder
    return feeder


def make_pet(hub, pet_id, name):
    pet = Pet({"id": pet_id, "name": name, "memberId": 1, "device_id": f"ha-pet-{pet_id}"}, hub)
    pet.saved_to_options = True
    hub.pets[str(pet_id)] = pet
    return pet


# --- discovery and platform -------------------------------------------------------------

def test_duo_maps_to_vision_and_event_platform_is_forwarded():
    assert product_name_map["Granary 2 Vision Duo"] is Granary2VisionFeeder
    assert "event" in PLATFORMS


def test_translation_files_are_valid_json_and_name_every_new_entity():
    base = Path(__file__).resolve().parent.parent / "custom_components/petlibro"
    for path in (base / "strings.json", base / "translations/en.json"):
        entity = json.loads(path.read_text())["entity"]
        for key in ("today_consumed_food_weight", "today_meal_count", "today_meal_duration",
                    "last_meal_time", "last_meal_pet", "today_feeder_intake", "today_feeder_meal_count",
                    "today_feeder_eating_time", "last_feeder_meal_time"):
            assert entity["sensor"][key]["name"], (path.name, key)
        assert entity["event"]["meal"]["name"] == "Meal"
        assert entity["event"]["notification"]["name"] == "Notification"


# --- API paging -------------------------------------------------------------------------

async def test_meal_record_paging_dedupes_and_skips_other_types():
    api = PetLibroAPI.__new__(PetLibroAPI)
    page1 = [{"workRecords": [
        {"id": i, "type": "MEAL_RECORD", "recordTime": 10_000 - i} for i in range(1, 26)
    ]}]
    page2 = [{"workRecords": [
        {"id": 25, "type": "MEAL_RECORD", "recordTime": 9_975},  # overlap with page 1
        {"id": 99, "type": "GRAIN_OUTPUT_SUCCESS", "recordTime": 9_000},  # returned despite the filter
        {"id": 100, "type": "MEAL_RECORD", "recordTime": 8_000},
    ]}]
    api.session = SimpleNamespace(request=AsyncMock(side_effect=[page1, page2]))
    records = await api.get_device_meal_records("sn", 1_000, 10_000)
    assert sorted(record["id"] for record in records) == list(range(1, 26)) + [100]
    second_call = api.session.request.await_args_list[1]
    assert second_call.kwargs["json"]["endTime"] == 9_975
    assert second_call.kwargs["json"]["type"] == ["MEAL_RECORD"]


async def test_meal_record_paging_handles_empty_and_rejects_malformed():
    api = PetLibroAPI.__new__(PetLibroAPI)
    api.session = SimpleNamespace(request=AsyncMock(return_value={}))
    assert await api.get_device_meal_records("sn", 0, 10) == []
    api.session = SimpleNamespace(request=AsyncMock(return_value=[{"workRecords": [{"id": 1}]}]))
    with pytest.raises(PetLibroAPIError):
        await api.get_device_meal_records("sn", 0, 10)


async def test_messages_are_cached_for_ten_seconds():
    api = PetLibroAPI.__new__(PetLibroAPI)
    api._last_api_call_times, api._cached_responses = {}, {}
    api.session = SimpleNamespace(get=AsyncMock(return_value=[{"id": 1}]))
    assert await api.device_messages() == [{"id": 1}]
    assert await api.device_messages() == [{"id": 1}]
    api.session.get.assert_awaited_once_with("/device/msg/list")


# --- feeder meals -------------------------------------------------------------------------

async def test_meals_split_per_pet(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    make_pet(hub, HORUSZ, "Horusz")
    make_pet(hub, MARGIE, "Margie")
    hub.api.get_device_meal_records.return_value = [
        # Horusz alone: petsInfo repeats the visit's intake.
        meal_record(1, today_at(7, 0), 66, 12.0, [{"petId": HORUSZ, "intake": 12.0}]),
        # Shared visit: the camera split it per pet.
        meal_record(2, today_at(12, 0), 200, 20.0,
                    [{"petId": HORUSZ, "intake": 5.0, "duration": 50}, {"petId": MARGIE, "intake": 15.0, "duration": 150}]),
        # Nobody recognized.
        meal_record(3, today_at(18, 30), 30, 4.5, [{"petId": -1, "intake": 4.5}]),
        # No petsInfo at all.
        meal_record(4, today_at(19, 0), 10, 1.0, []),
    ]
    await feeder.refresh()

    start, end = hub.api.get_device_meal_records.await_args.args[1:]
    assert start == ms(dt_util.start_of_local_day())
    assert end >= start

    meals = feeder.today_meals
    assert [meal["id"] for meal in meals] == [1, 2, 3, 4]
    assert meals[0]["seconds"] == 66 and meals[0]["pets"][0]["seconds"] == 66
    assert [share["name"] for share in meals[1]["pets"]] == ["Horusz", "Margie"]
    assert meals[2]["pets"][0]["name"] == "Unknown"
    assert feeder.today_consumed_food_weight == 37.5
    assert feeder.today_meal_count == 4
    assert feeder.today_meal_duration == 66 + 200 + 30 + 10
    assert feeder.last_meal_pet == "Unknown"  # meal 4 had no petsInfo
    assert feeder.last_meal_time == today_at(19, 0)

    horusz = feeder.pet_meals_today(HORUSZ)
    assert [(meal["grams"], meal["seconds"]) for meal in horusz] == [(12.0, 66), (5.0, 50)]


async def test_failed_read_makes_totals_unknown_but_keeps_last_meal(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    make_pet(hub, HORUSZ, "Horusz")
    hub.api.get_device_meal_records.return_value = [
        meal_record(1, today_at(7, 0), 66, 12.0, [{"petId": HORUSZ, "intake": 12.0}]),
    ]
    await feeder.refresh()
    hub.api.get_device_meal_records.side_effect = PetLibroAPIError("boom")
    await feeder.refresh()
    assert feeder.today_meals is None
    assert feeder.today_consumed_food_weight is None
    assert feeder.today_meal_count is None
    assert feeder.last_meal_pet == "Horusz"
    assert feeder.last_meal_by_pet[HORUSZ]["grams"] == 12.0


async def test_records_from_yesterday_are_not_today(hass, monkeypatch):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    await feeder.refresh()
    assert feeder.today_meals == []
    feeder._data["mealRecords"]["day"] = "2000-01-01"
    assert feeder.today_meals is None


# --- pets -----------------------------------------------------------------------------------

async def test_pet_totals_across_two_feeders(hass):
    hub = make_hub(hass)
    kitchen, office = make_feeder(hub, "vision-1", "Kitchen"), make_feeder(hub, "vision-2", "Office Duo")
    horusz, margie = make_pet(hub, HORUSZ, "Horusz"), make_pet(hub, MARGIE, "Margie")
    hub.api.get_device_meal_records.return_value = [
        meal_record(1, today_at(7, 0), 66, 12.0, [{"petId": HORUSZ, "intake": 12.0}]),
    ]
    await kitchen.refresh()
    hub.api.get_device_meal_records.return_value = [
        meal_record(2, today_at(9, 0), 120, 9.0, [{"petId": HORUSZ, "intake": 9.0}]),
        meal_record(3, today_at(9, 30), 60, 7.0, [{"petId": MARGIE, "intake": 7.0}]),
    ]
    await office.refresh()

    assert horusz.today_feeder_intake == 21.0
    assert horusz.today_feeder_meal_count == 2
    assert horusz.today_feeder_eating_time == 186
    assert horusz.last_feeder_meal["feeder"] == "Office Duo"
    assert horusz.last_feeder_meal_time == today_at(9, 0)
    assert margie.today_feeder_intake == 7.0

    hub.api.get_device_meal_records.side_effect = PetLibroAPIError("boom")
    await office.refresh()
    assert horusz.today_feeder_intake is None  # never a smaller, partial total
    assert horusz.last_feeder_meal["grams"] == 9.0


async def test_pet_meal_sensors_only_exist_with_a_vision_feeder(hass):
    hub = make_hub(hass)
    pet = make_pet(hub, HORUSZ, "Horusz")
    meal_keys = {"today_feeder_intake", "today_feeder_meal_count", "today_feeder_eating_time", "last_feeder_meal_time"}
    keys = {d.key for d in PET_ENTITY_MAP[PL_PetSensorEntity] if d.create_fn(pet)}
    assert not keys & meal_keys
    make_feeder(hub)
    keys = {d.key for d in PET_ENTITY_MAP[PL_PetSensorEntity] if d.create_fn(pet)}
    assert meal_keys <= keys


async def test_pet_sensor_entity_values(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    pet = make_pet(hub, HORUSZ, "Horusz")
    hub.api.get_device_meal_records.return_value = [
        meal_record(1, today_at(7, 0), 66, 12.0, [{"petId": HORUSZ, "intake": 12.0}]),
    ]
    await feeder.refresh()
    by_key = {d.key: d for d in PET_ENTITY_MAP[PL_PetSensorEntity]}
    intake = PL_PetSensorEntity(pet, hub, by_key["today_feeder_intake"])
    assert intake.native_value == 12.0
    assert intake.native_unit_of_measurement == "g"
    assert intake.extra_state_attributes["meals"][0]["seconds"] == 66
    last = PL_PetSensorEntity(pet, hub, by_key["last_feeder_meal_time"])
    assert last.native_value == today_at(7, 0)
    assert last.extra_state_attributes == {"grams": 12.0, "seconds": 66, "feeder": "Kitchen Vision"}


# --- feeder sensor entities -------------------------------------------------------------------

async def test_feeder_sensor_entities(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    make_pet(hub, HORUSZ, "Horusz")
    make_pet(hub, MARGIE, "Margie")
    hub.api.get_device_meal_records.return_value = [
        meal_record(2, today_at(12, 0), 200, 20.0, [{"petId": HORUSZ, "intake": 5.0}, {"petId": MARGIE, "intake": 15.0}]),
    ]
    await feeder.refresh()
    by_key = {d.key: d for d in DEVICE_SENSOR_MAP[Granary2VisionFeeder]}
    eaten = PetLibroSensorEntity(feeder, hub, by_key["today_consumed_food_weight"])
    assert eaten.native_value == 20.0
    assert eaten.extra_state_attributes["meals"][0]["pets"][1] == {"name": "Margie", "grams": 15.0, "seconds": None}
    who = PetLibroSensorEntity(feeder, hub, by_key["last_meal_pet"])
    assert who.native_value == "Horusz, Margie"
    assert who.extra_state_attributes["grams"] == 20.0
    when = PetLibroSensorEntity(feeder, hub, by_key["last_meal_time"])
    assert when.native_value == today_at(12, 0)


# --- notifications ----------------------------------------------------------------------------

async def test_notifications_are_matched_by_serial(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    hub.api.device_messages.return_value = [
        {"id": 7, "eventKey": "DETECTION_MOVE", "timestamp": ms(today_at(8, 0)),
         "param": json.dumps({"deviceSn": "vision-1", "petName": "Horusz"}), "imageUrl": None,
         "title": "Motion", "content": "Motion detected"},
        {"id": 8, "eventKey": "DETECTION_MOVE", "timestamp": ms(today_at(8, 1)),
         "param": json.dumps({"deviceSn": "other-feeder"})},
        {"id": 9, "eventKey": "X", "timestamp": "bad", "param": "{}"},
    ]
    await feeder.refresh()
    [notification] = feeder.notifications
    assert notification["id"] == 7
    assert notification["details"] == {"petName": "Horusz"}
    assert notification["message"] == "Motion detected" and "image_url" not in notification


# --- event entities ---------------------------------------------------------------------------

def make_event_entity(feeder, hub, key):
    description = next(d for d in DEVICE_EVENT_MAP[Granary2VisionFeeder] if d.key == key)
    entity = PetLibroEventEntity(feeder, hub, description)
    entity.async_write_ha_state = Mock()
    return entity


async def test_meal_event_fires_once_per_new_finished_meal(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    make_pet(hub, HORUSZ, "Horusz")
    records = [meal_record(1, today_at(7, 0), 66, 12.0, [{"petId": HORUSZ, "intake": 12.0}])]
    hub.api.get_device_meal_records.return_value = records
    await feeder.refresh()
    entity = make_event_entity(feeder, hub, "meal")

    entity._process_items()  # fresh install: existing meals don't fire
    assert entity.async_write_ha_state.call_count == 0

    unfinished = meal_record(2, dt_util.now() - timedelta(minutes=1), 30, 3.0, [{"petId": HORUSZ, "intake": 3.0}])
    unfinished.pop("eatingEndTime")
    records.append(unfinished)
    await feeder.refresh()
    entity._process_items()
    assert entity.async_write_ha_state.call_count == 0  # still eating

    records[1] = meal_record(2, dt_util.now() - timedelta(minutes=1), 30, 3.0, [{"petId": HORUSZ, "intake": 3.0}])
    await feeder.refresh()
    entity._process_items()
    entity._process_items()
    assert entity.async_write_ha_state.call_count == 1
    attributes = entity.state_attributes
    assert attributes["event_type"] == "meal"
    assert attributes["pet_names"] == ["Horusz"]
    assert attributes["grams"] == 3.0 and attributes["seconds"] == 30
    assert attributes["record_time"]


async def test_meal_event_catches_up_after_restart(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    hub.api.get_device_meal_records.return_value = [
        meal_record(1, today_at(7, 0), 60, 10.0, [{"petId": -1, "intake": 10.0}]),
        meal_record(2, today_at(8, 0), 60, 11.0, [{"petId": -1, "intake": 11.0}]),
    ]
    await feeder.refresh()
    entity = make_event_entity(feeder, hub, "meal")
    entity._last_record_ms = ms(today_at(7, 1))  # what the restored state carries
    entity._process_items()
    assert entity.async_write_ha_state.call_count == 1
    assert entity.state_attributes["record_id"] == 2
    assert entity.state_attributes["pet_names"] == ["Unknown"]


async def test_notification_event(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    messages = []
    hub.api.device_messages.return_value = messages
    await feeder.refresh()
    entity = make_event_entity(feeder, hub, "notification")
    entity._process_items()
    messages.append({"id": 11, "eventKey": "FOOD_LOW", "timestamp": ms(dt_util.now()),
                     "param": json.dumps({"deviceSn": "vision-1"})})
    await feeder.refresh()
    entity._process_items()
    assert entity.async_write_ha_state.call_count == 1
    assert entity.state_attributes["event_key"] == "FOOD_LOW"
    assert entity.state_attributes["event_type"] == "notification"


# Two real Vision Duo records (2026-10-23 and 10-24), serial and photo links removed.
REAL_RECORDS = [
    {"id": "f18b646b42cf4c37987a3f3c37b183c6", "type": "MEAL_RECORD", "eventType": "PET_EATING_RECORD_EVENT",
     "recordTime": 1792762208069, "duration": 2, "intake": 7, "petIds": [1703837, 705615], "petName": "Margie,Horusz",
     "petsInfo": [{"petId": 1703837, "petName": "Margie", "intake": 7, "duration": 157, "speed": "SLOW"}],
     "showUpPetsInfo": [{"petId": 705615, "petName": "Horusz", "intake": 0, "duration": 116, "speed": None}],
     "petEnterTime": 1792762208069, "petEatingStartTime": 1792762208069, "eatingEndTime": 1792762380426},
    {"id": "11d3442f92ad41419b7f508a64d6c11e", "type": "MEAL_RECORD", "eventType": "PET_EATING_RECORD_EVENT",
     "recordTime": 1792848613676, "duration": 2, "intake": 14, "petIds": [-1, 705615, 1703837],
     "petsInfo": [{"petId": 705615, "petName": "Horusz", "intake": 10, "duration": 173},
                  {"petId": 1703837, "petName": "Margie", "intake": 4, "duration": 94}],
     "showUpPetsInfo": [{"petId": -1, "petName": "Unknown pet", "intake": 0, "duration": 5}],
     "petEnterTime": 1792848613676, "petEatingStartTime": 1792848613676, "eatingEndTime": 1792848788879},
]


async def test_real_vision_duo_records(hass):
    hub = make_hub(hass)
    feeder = make_feeder(hub)
    # Pets come from the sharing account, so names fall back to the record's petName.
    feeder._data["mealRecords"] = {"day": dt_util.now().date().isoformat(), "records": REAL_RECORDS}
    first, second = feeder.today_meals
    assert first["seconds"] == 172 and first["grams"] == 7.0
    assert first["pets"] == [{"pet_id": 1703837, "name": "Margie", "grams": 7.0, "seconds": 157.0}]
    assert first["show_ups"] == [{"pet_id": 705615, "name": "Horusz", "grams": 0.0, "seconds": 116.0}]
    assert [(p["name"], p["grams"], p["seconds"]) for p in second["pets"]] == [("Horusz", 10.0, 173.0), ("Margie", 4.0, 94.0)]
    assert second["show_ups"][0]["name"] == "Unknown"
    assert feeder.today_consumed_food_weight == 21.0
    assert [m["grams"] for m in feeder.pet_meals_today(705615)] == [10.0]
    assert [m["seconds"] for m in feeder.pet_meals_today(1703837)] == [157.0, 94.0]
