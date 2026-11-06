"""Routing live updates and refreshing devices."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petlibro.exceptions import PetLibroAPIError
from custom_components.petlibro.push.dispatcher import DeviceRefresher, PushDispatcher
from custom_components.petlibro.push.messages import parse_message

from .fakes import push_payload


def message(command="APP_REFRESH_EVENT", topic="dl/PLAF109/SN1/app/event/sub"):
    return parse_message(topic, push_payload(command))


async def test_handlers_run_for_their_command_only(hass):
    dispatcher = PushDispatcher()
    refresh, other = AsyncMock(), AsyncMock()
    dispatcher.register("APP_REFRESH_EVENT", refresh)
    remove = dispatcher.register("OTHER", other)
    await dispatcher.async_dispatch(message())
    refresh.assert_awaited_once()
    other.assert_not_awaited()
    remove()
    await dispatcher.async_dispatch(message("OTHER"))
    other.assert_not_awaited()


async def test_unknown_commands_are_reported_once_at_info(hass, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.petlibro.push.dispatcher")
    dispatcher = PushDispatcher()
    for _ in range(3):
        await dispatcher.async_dispatch(message("PET_VISIT"))
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "PET_VISIT" in r.getMessage()]
    assert len(infos) == 1


def make_hub(hass, device):
    coordinator = SimpleNamespace(async_update_listeners=MagicMock())
    return SimpleNamespace(devices={"SN1": device}, last_refresh_times={}, coordinator=coordinator)


async def test_a_burst_of_pushes_becomes_one_refresh_plus_one_trailing(hass):
    entry = MockConfigEntry(domain="petlibro")
    entry.add_to_hass(hass)
    device = SimpleNamespace(name="Wet Food Feeder", refresh=AsyncMock())
    hub = make_hub(hass, device)
    refresher = DeviceRefresher(hass, entry, hub, cooldown=0.05)
    for _ in range(5):
        await refresher.async_handle(message())
    await hass.async_block_till_done()
    assert device.refresh.await_count == 1  # the first push refreshes at once
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    assert device.refresh.await_count == 2  # the rest become one refresh after the cooldown
    assert hub.coordinator.async_update_listeners.call_count == 2
    assert "SN1" in hub.last_refresh_times
    refresher.async_shutdown()


async def test_pushes_for_unknown_devices_and_the_account_are_ignored(hass):
    entry = MockConfigEntry(domain="petlibro")
    entry.add_to_hass(hass)
    device = SimpleNamespace(name="Feeder", refresh=AsyncMock())
    refresher = DeviceRefresher(hass, entry, make_hub(hass, device), cooldown=0.01)
    await refresher.async_handle(message(topic="dl/PLAF205/UNKNOWN/app/event/sub"))
    await refresher.async_handle(message(topic="dl/member/1/sub"))
    await hass.async_block_till_done()
    device.refresh.assert_not_awaited()


async def test_a_failed_refresh_is_logged_and_does_not_notify(hass, caplog):
    entry = MockConfigEntry(domain="petlibro")
    entry.add_to_hass(hass)
    device = SimpleNamespace(name="Feeder", refresh=AsyncMock(side_effect=PetLibroAPIError("cloud down")))
    hub = make_hub(hass, device)
    refresher = DeviceRefresher(hass, entry, hub, cooldown=0.01)
    await refresher.async_handle(message())
    await hass.async_block_till_done()
    hub.coordinator.async_update_listeners.assert_not_called()
    assert "refreshing Feeder failed" in caplog.text
    refresher.async_shutdown()
