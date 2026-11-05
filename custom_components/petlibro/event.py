"""Support for PETLIBRO event entities: meals and app notifications."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from logging import getLogger
from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .devices import Device
from .devices.feeders.granary_2_vision_feeder import UNKNOWN_PET_NAME, Granary2VisionFeeder
from .entity import PetLibroEntity, PetLibroEntityDescription, _DeviceT
from .hub import PetLibroHub

_LOGGER = getLogger(__name__)

# Stored with every event, so a restart can tell which records it already fired.
ATTR_RECORD_TIME = "record_time"

# Record ids remembered per entity so the same record never fires twice.
MAX_SEEN_IDS = 500

# A meal record without an end time is treated as finished once it is this old.
MEAL_SETTLE_TIME = timedelta(minutes=10)


@dataclass(frozen=True)
class PetLibroEventEntityDescription(EventEntityDescription, PetLibroEntityDescription[_DeviceT]):
    """Describes an event entity that fires once per new item (meal record, message)."""

    # The device's current items, each with "id" and "record_ms", or None when unread.
    items_fn: Callable[[_DeviceT], list[dict[str, Any]] | None] = lambda _: None
    # Whether an item is final and can fire (a meal still in progress waits).
    ready_fn: Callable[[dict[str, Any]], bool] = lambda _: True
    event_type_fn: Callable[[dict[str, Any]], str] = lambda _: ""
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda _: {}


class PetLibroEventEntity(PetLibroEntity[_DeviceT], EventEntity):
    """Fires once for each new item the cloud reports for a device.

    On a fresh install nothing fires for items that already exist. After a restart,
    items newer than the last fired one (restored from the entity's state) fire once,
    so meals eaten while Home Assistant was down still reach the history.
    """

    entity_description: PetLibroEventEntityDescription[_DeviceT]

    def __init__(self, device, hub, description) -> None:
        """Initialize the event entity."""
        super().__init__(device, hub, description)
        self._seen_ids: dict[Any, None] = {}
        self._last_record_ms: int | None = None
        self._baseline_done = False

    async def async_added_to_hass(self) -> None:
        """Restore the last fired record time, then process the current items."""
        await super().async_added_to_hass()
        restored = self.state_attributes.get(ATTR_RECORD_TIME)
        if isinstance(restored, str) and (restored_time := dt_util.parse_datetime(restored)):
            self._last_record_ms = int(restored_time.timestamp() * 1000)
        self._process_items()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire events for new items after each refresh."""
        self._process_items()
        super()._handle_coordinator_update()

    @callback
    def _process_items(self) -> None:
        description = self.entity_description
        items = description.items_fn(self.device)
        if items is None:
            return
        ready = [item for item in items if description.ready_fn(item)]

        if not self._baseline_done:
            self._baseline_done = True
            new_items = (
                []
                if self._last_record_ms is None
                else [item for item in ready if item["record_ms"] > self._last_record_ms]
            )
        else:
            new_items = [item for item in ready if item["id"] not in self._seen_ids]

        for item in ready:
            self._seen_ids[item["id"]] = None
        while len(self._seen_ids) > MAX_SEEN_IDS:
            self._seen_ids.pop(next(iter(self._seen_ids)))

        for item in sorted(new_items, key=lambda item: item["record_ms"]):
            event_type = description.event_type_fn(item)
            attributes = {
                **description.attributes_fn(item),
                ATTR_RECORD_TIME: dt_util.utc_from_timestamp(item["record_ms"] / 1000).isoformat(),
            }
            _LOGGER.debug("%s %s: %s %s", self.device.name, self.key, event_type, attributes)
            self._trigger_event(event_type, attributes)
            self._last_record_ms = max(self._last_record_ms or 0, item["record_ms"])
            self.async_write_ha_state()


def _meal_ready(meal: dict[str, Any]) -> bool:
    return meal["end"] is not None or dt_util.now() - meal["time"] > MEAL_SETTLE_TIME


def _meal_attributes(meal: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": meal["id"],
        "started_at": meal["time"].isoformat(),
        "ended_at": meal["end"].isoformat() if meal["end"] else None,
        "grams": meal["grams"],
        "seconds": meal["seconds"],
        "pet_names": [share["name"] for share in meal["pets"]] or [UNKNOWN_PET_NAME],
        "pets": [
            {
                "pet_id": share["pet_id"],
                "name": share["name"],
                "grams": share["grams"],
                "seconds": share["seconds"],
            }
            for share in meal["pets"]
        ],
    }


def _notification_attributes(notification: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": notification["id"],
        "event_key": notification["event_key"],
        "sent_at": notification["time"].isoformat(),
        **{
            key: notification[key]
            for key in ("title", "message", "image_url")
            if key in notification
        },
        "details": notification["details"],
    }


DEVICE_EVENT_MAP: dict[type[Device], list[PetLibroEventEntityDescription]] = {
    Granary2VisionFeeder: [
        PetLibroEventEntityDescription[Granary2VisionFeeder](
            key="meal",
            translation_key="meal",
            icon="mdi:food-drumstick",
            name="Meal",
            event_types=["meal"],
            items_fn=lambda device: device.today_meals,
            ready_fn=_meal_ready,
            event_type_fn=lambda _: "meal",
            attributes_fn=_meal_attributes,
        ),
        PetLibroEventEntityDescription[Granary2VisionFeeder](
            key="notification",
            translation_key="notification",
            icon="mdi:message-badge-outline",
            name="Notification",
            event_types=["notification"],
            items_fn=lambda device: device.notifications,
            event_type_fn=lambda _: "notification",
            attributes_fn=_notification_attributes,
        ),
    ],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PETLIBRO event entities using config entry."""
    hub: PetLibroHub = hass.data[DOMAIN].get(entry.entry_id)
    if not hub or not hub.devices:
        return

    entities = [
        PetLibroEventEntity(device, hub, description)
        for device in hub.devices.values()
        for device_type, descriptions in DEVICE_EVENT_MAP.items()
        if isinstance(device, device_type)
        for description in descriptions
    ]
    if entities:
        async_add_entities(entities)
