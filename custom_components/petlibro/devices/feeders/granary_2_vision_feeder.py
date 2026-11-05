import aiohttp
from logging import getLogger

from datetime import datetime
import json
from typing import Any

from homeassistant.util import dt as dt_util

from ...exceptions import PetLibroAPIError
from .granary_smart_camera_feeder import GranarySmartCameraFeeder

_LOGGER = getLogger(__name__)

# petsInfo uses this petId for the share of a visit the camera could not match to a pet.
UNKNOWN_PET_ID = -1
UNKNOWN_PET_NAME = "Unknown"


def _numeric_value(value: object) -> float | None:
    """Return an API number as a float without treating booleans as numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch_ms(value: object) -> int | None:
    """Return an epoch-milliseconds API value as an int, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _local_time(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to an aware datetime in Home Assistant's time zone."""
    return dt_util.as_local(dt_util.utc_from_timestamp(epoch_ms / 1000))


def _json_object(value: object) -> dict[str, Any]:
    """Return a JSON-encoded API object as a dict ({} when absent or invalid)."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Granary2VisionFeeder(GranarySmartCameraFeeder):
    """Represents the Granary 2 Vision feeder.

    Subclass of GranarySmartCameraFeeder so the device loads and
    reuses its entities. Can be split into a standalone class if desired.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the feeder."""
        super().__init__(*args, **kwargs)
        # The latest meal overall and per pet id; kept past midnight so they don't go unknown.
        self._last_meal: dict[str, Any] | None = None
        self.last_meal_by_pet: dict[int, dict[str, Any]] = {}

    async def refresh(self):
        """Refresh the device data from the API."""
        await super().refresh()
        try:
            free_feeding_setting = await self.api.device_get_free_feeding_setting(self.serial)
            self.update_data({
                "freeFeedingSetting": free_feeding_setting or {},
            })
        except PetLibroAPIError as err:
            _LOGGER.error(f"Error refreshing free feeding setting for Granary2VisionFeeder: {err}")

        # Isolated in its own error handler: TUTK credential retrieval failing
        # (e.g. no camera entitlement) shouldn't disrupt the rest of the refresh.
        try:
            tutk_info = await self.api.device_tutk_info(self.serial)
            self.update_data({
                "tutkInfo": tutk_info or {},
            })
        except PetLibroAPIError as err:
            _LOGGER.error(f"Error refreshing TUTK camera info for Granary2VisionFeeder: {err}")

        try:
            data_real_info = await self.api.device_data_real_info(self.serial)
            self.update_data({
                "dataRealInfo": data_real_info or {},
            })
        except PetLibroAPIError as err:
            self.update_data({"dataRealInfo": {}})
            _LOGGER.error(f"Error refreshing data real info for Granary2VisionFeeder: {err}")

        # Today's meals: one record per visit, weighed by the bowl scale and split per pet by
        # the camera. A failed read clears them, so totals go unknown instead of going stale.
        try:
            day_start = dt_util.start_of_local_day()
            records = await self.api.get_device_meal_records(
                self.serial,
                int(day_start.timestamp() * 1000),
                int(dt_util.utcnow().timestamp() * 1000),
            )
            self.update_data({"mealRecords": {"day": day_start.date().isoformat(), "records": records}})
            if meals := self.today_meals:
                self._last_meal = meals[-1]
                for meal in meals:  # oldest first, so each pet ends on its latest meal
                    for share in meal["pets"]:
                        self.last_meal_by_pet[share["pet_id"]] = {
                            "time": meal["time"],
                            "grams": share["grams"],
                            "seconds": share["seconds"],
                            "feeder": self.name,
                        }
            if records:
                newest = max(records, key=lambda record: record["recordTime"])
                _LOGGER.debug(
                    "Newest meal record for %s: fields %s, numbers %s, petsInfo %s",
                    self.name,
                    sorted(newest),
                    {k: v for k, v in newest.items() if isinstance(v, (int, float)) and k != "deviceSn"},
                    newest.get("petsInfo"),
                )
        except (PetLibroAPIError, aiohttp.ClientError) as err:
            self.update_data({"mealRecords": {}})
            _LOGGER.warning("Unable to refresh meal records for %s: %s", self.name, err)

        # The app's message center (alerts and notifications). Account-wide, cached by the API.
        try:
            self.update_data({"messages": await self.api.device_messages()})
        except (PetLibroAPIError, aiohttp.ClientError) as err:
            _LOGGER.warning("Unable to refresh the message center for %s: %s", self.name, err)

    # --- Meals (MEAL_RECORD work records)

    def _pet_name(self, pet_id: int, share: dict[str, Any]) -> str:
        """Return a pet's name: the account's pet profile, else the record's own petName."""
        if pet_id == UNKNOWN_PET_ID:
            return UNKNOWN_PET_NAME
        if pet := (getattr(self.hub, "pets", None) or {}).get(str(pet_id)):
            return pet.name
        name = share.get("petName")
        return name if isinstance(name, str) and name else f"Pet {pet_id}"

    def _parse_shares(self, shares: object) -> list[dict[str, Any]]:
        """Parse petsInfo / showUpPetsInfo: one entry per pet, grams and seconds of its own."""
        parsed = []
        for share in shares if isinstance(shares, list) else []:
            if not isinstance(share, dict):
                continue
            pet_id = share.get("petId")
            if isinstance(pet_id, bool) or not isinstance(pet_id, int):
                pet_id = UNKNOWN_PET_ID
            parsed.append({
                "pet_id": pet_id,
                "name": self._pet_name(pet_id, share),
                "grams": _numeric_value(share.get("intake")),
                # Seconds: 157 here is the "02m37s" in the same entry's text.
                "seconds": _numeric_value(share.get("duration")),
            })
        return parsed

    def _parse_meal(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Turn a MEAL_RECORD into a meal: who ate, when, how much and for how long.

        `petsInfo` lists the pets that ate (each with its own grams and seconds; their
        grams add up to the visit's `intake`). `showUpPetsInfo` lists pets that came to
        the bowl during the visit without eating ("Horusz show up", petId -1 when not
        recognized).
        """
        record_ms = _epoch_ms(record.get("recordTime"))
        if record_ms is None:
            return None
        start_ms = _epoch_ms(record.get("petEatingStartTime"))
        end_ms = _epoch_ms(record.get("eatingEndTime"))
        # The top-level `duration` is whole minutes (2 for "02m52s"), so the visit's
        # length comes from its start and end times.
        seconds = (
            round((end_ms - start_ms) / 1000)
            if start_ms is not None and end_ms is not None and end_ms >= start_ms
            else None
        )
        grams = _numeric_value(record.get("intake"))

        pets = self._parse_shares(record.get("petsInfo"))
        if len(pets) == 1:
            # One pet ate: fill in from the visit only what its own entry lacks.
            if pets[0]["grams"] is None:
                pets[0]["grams"] = grams
            if pets[0]["seconds"] is None:
                pets[0]["seconds"] = seconds

        return {
            "id": record.get("id"),
            "record_ms": record_ms,
            "time": _local_time(start_ms if start_ms is not None else record_ms),
            "end": _local_time(end_ms) if end_ms is not None else None,
            "grams": grams,
            "seconds": seconds,
            "pets": pets,
            "show_ups": self._parse_shares(record.get("showUpPetsInfo")),
        }

    @property
    def today_meals(self) -> list[dict[str, Any]] | None:
        """Return today's meals, oldest first, or None when they could not be read today."""
        meal_data = self._data.get("mealRecords") or {}
        if meal_data.get("day") != dt_util.now().date().isoformat():
            return None
        meals = [meal for record in meal_data.get("records") or [] if (meal := self._parse_meal(record))]
        meals.sort(key=lambda meal: meal["time"])
        return meals

    @property
    def last_meal(self) -> dict[str, Any] | None:
        """Return the most recent meal seen since Home Assistant started (survives midnight)."""
        return self._last_meal

    def pet_meals_today(self, pet_id: int) -> list[dict[str, Any]] | None:
        """Return one pet's shares of today's meals, or None when meals could not be read."""
        if (meals := self.today_meals) is None:
            return None
        return [
            {"time": meal["time"], "grams": share["grams"], "seconds": share["seconds"], "feeder": self.name}
            for meal in meals
            for share in meal["pets"]
            if share["pet_id"] == pet_id
        ]

    @property
    def today_consumed_food_weight(self) -> float | None:
        """Return the grams eaten from this feeder today (every visit, recognized or not)."""
        if (meals := self.today_meals) is None or any(meal["grams"] is None for meal in meals):
            return None
        return round(sum(meal["grams"] for meal in meals), 1)

    @property
    def today_meal_count(self) -> int | None:
        """Return the number of visits that ate from this feeder today."""
        meals = self.today_meals
        return None if meals is None else len(meals)

    @property
    def today_meal_duration(self) -> int | None:
        """Return the seconds spent eating from this feeder today."""
        if (meals := self.today_meals) is None or any(meal["seconds"] is None for meal in meals):
            return None
        return sum(meal["seconds"] for meal in meals)

    @property
    def last_meal_time(self) -> datetime | None:
        """Return when the most recent meal started."""
        return meal["time"] if (meal := self.last_meal) else None

    @property
    def last_meal_pet(self) -> str | None:
        """Return who ate the most recent meal (several names for a shared visit)."""
        if not (meal := self.last_meal):
            return None
        return ", ".join(share["name"] for share in meal["pets"]) or UNKNOWN_PET_NAME

    # --- Message center (app notifications)

    @property
    def notifications(self) -> list[dict[str, Any]] | None:
        """Return this feeder's message-center notifications, oldest first.

        None until the message center has been read once. The message list is
        account-wide, so messages are matched to this feeder by the deviceSn inside
        each message's JSON `param`.
        """
        if "messages" not in self._data:
            return None
        notifications = []
        for message in self._data.get("messages") or []:
            if not isinstance(message, dict):
                continue
            param = _json_object(message.get("param"))
            timestamp = _epoch_ms(message.get("timestamp"))
            message_id = message.get("id")
            if param.get("deviceSn") != self.serial or timestamp is None or message_id in (None, ""):
                continue
            notification = {
                "id": message_id,
                "record_ms": timestamp,
                "time": _local_time(timestamp),
                "event_key": message.get("eventKey"),
                "details": {key: value for key, value in param.items() if key != "deviceSn"},
            }
            for api_key, key in (("title", "title"), ("content", "message"), ("imageUrl", "image_url")):
                if isinstance(value := message.get(api_key), str) and value:
                    notification[key] = value
            notifications.append(notification)
        notifications.sort(key=lambda notification: notification["record_ms"])
        return notifications

    @property
    def night_vision(self) -> str:
        """Return the current night vision mode.

        Unlike the Granary Smart Camera Feeder, this device reports night
        vision under getAttributeSetting.nightVisionMode; realInfo.nightVision
        is always null.
        """
        return self._data.get("getAttributeSetting", {}).get("nightVisionMode", "unknown")

    @property
    def left_food_low(self) -> bool | None:
        """Return True if the left grain warehouse is low, or None if this unit doesn't report it."""
        if not self.supports_dual_bowl:
            return None
        value = self._data.get("realInfo", {}).get("leftWarehouseSurplusGrain")
        if value is None:
            return None
        return not bool(value)

    @property
    def right_food_low(self) -> bool | None:
        """Return True if the right grain warehouse is low, or None if this unit doesn't report it."""
        if not self.supports_dual_bowl:
            return None
        value = self._data.get("realInfo", {}).get("rightWarehouseSurplusGrain")
        if value is None:
            return None
        return not bool(value)

    @property
    def pet_detection_enabled(self) -> bool:
        """Return whether AI pet detection is enabled."""
        return bool(self._data.get("getAttributeSetting", {}).get("petDetectionSwitch", False))

    @property
    def human_detection_enabled(self) -> bool:
        """Return whether AI human detection is enabled."""
        return bool(self._data.get("realInfo", {}).get("enableHumanDetection", False))

    @property
    def talk_channel_active(self) -> bool:
        """Return whether a 2-way talk session is currently active."""
        return bool(self._data.get("realInfo", {}).get("talkChannelState", False))

    @property
    def radar_sensing_level(self) -> str:
        """Return the current radar sensing/trigger level."""
        return self._data.get("realInfo", {}).get("radarSensingLevel", "unknown")

    @property
    def free_feeding_per_grain(self) -> float | None:
        """Return the amount dispensed per Free Feeding (Smart Feed) top-up."""
        value = self._data.get("freeFeedingSetting", {}).get("freePerGrainNum")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def free_feeding_daily_max(self) -> float | None:
        """Return the max number of Free Feeding top-ups allowed per day."""
        value = self._data.get("freeFeedingSetting", {}).get("freeDailyMaxNum")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def free_feeding_leftover_weight(self) -> float | None:
        """Return the minimum food-left threshold that pauses Free Feeding."""
        value = self._data.get("freeFeedingSetting", {}).get("freeLeftoverWeight")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def free_feeding_wait_seconds(self) -> float | None:
        """Return the wait time (seconds) Free Feeding uses between checks."""
        value = self._data.get("freeFeedingSetting", {}).get("freeWaitSeconds")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def feeding_mode(self) -> str | None:
        """Return the device's current feeding mode: 'FREE' (Smart Feed) or 'PLAN' (schedule-based)."""
        return self._data.get("dataRealInfo", {}).get("feedingMode")

    @property
    def bowl_mode(self) -> str | None:
        """Return the configured bowl mode."""
        value = self._data.get("bowlMode")
        if not isinstance(value, str):
            value = self._data.get("dataRealInfo", {}).get("bowlMode")
        return value if isinstance(value, str) else None

    @property
    def supports_single_bowl(self) -> bool:
        """Return whether this product uses combined single-bowl telemetry."""
        return self.bowl_mode == "SINGLE_BOWL"

    @property
    def supports_dual_bowl(self) -> bool:
        """Return whether this product reports separate left/right bowl telemetry."""
        return self.bowl_mode not in (None, "SINGLE_BOWL")

    @property
    def remaining_food_weight(self) -> float | None:
        """Return the food remaining in a single bowl, in grams."""
        if not self.supports_single_bowl:
            return None
        return _numeric_value(self._data.get("dataRealInfo", {}).get("remainingGrain"))

    @property
    def left_remaining_food_weight(self) -> float | None:
        """Return the food remaining in the left bowl, in grams."""
        if not self.supports_dual_bowl:
            return None
        return _numeric_value(self._data.get("dataRealInfo", {}).get("leftRemainingGrain"))

    @property
    def right_remaining_food_weight(self) -> float | None:
        """Return the food remaining in the right bowl, in grams."""
        if not self.supports_dual_bowl:
            return None
        return _numeric_value(self._data.get("dataRealInfo", {}).get("rightRemainingGrain"))

    @property
    def auto_feed_max_weight(self) -> float | None:
        """Return the configured automatic-feed bowl maximum, in grams."""
        return _numeric_value(self._data.get("dataRealInfo", {}).get("autoFeedMaxWeight"))

    @property
    def max_feedable(self) -> float | None:
        """Return the number of additional portions feedable into a single bowl."""
        if not self.supports_single_bowl:
            return None
        return _numeric_value(self._data.get("dataRealInfo", {}).get("maxFeedable"))

    @property
    def left_max_feedable(self) -> float | None:
        """Return the number of additional portions feedable into the left bowl."""
        if not self.supports_dual_bowl:
            return None
        return _numeric_value(self._data.get("dataRealInfo", {}).get("leftMaxFeedable"))

    @property
    def right_max_feedable(self) -> float | None:
        """Return the number of additional portions feedable into the right bowl."""
        if not self.supports_dual_bowl:
            return None
        return _numeric_value(self._data.get("dataRealInfo", {}).get("rightMaxFeedable"))

    @property
    def portion_to_gram_ratio(self) -> float | None:
        """Return the number of grams represented by one feeder portion."""
        return _numeric_value(self._data.get("dataRealInfo", {}).get("portionToGramRatio"))

    @property
    def auto_stop_feed_enabled(self) -> bool | None:
        """Return whether feeding stops at the configured bowl maximum."""
        value = self._data.get("dataRealInfo", {}).get("autoStopFeedSwitch")
        return value if isinstance(value, bool) else None

    @property
    def camera_auth_info(self) -> str | None:
        """Return the Kalay/TUTK per-device camera auth info string.

        Not returned by /member/third/tutk/info (that endpoint only returns
        the account-level userToken/appTutkUrl) - this comes from realInfo.
        """
        return self._data.get("realInfo", {}).get("cameraAuthInfo")

    @property
    def tutk_user_token(self) -> str | None:
        """Return the Kalay/TUTK user token used to establish a P2P session."""
        return self._data.get("tutkInfo", {}).get("userToken")

    @property
    def tutk_app_url(self) -> str | None:
        """Return the Kalay/TUTK app URL/endpoint for this account."""
        return self._data.get("tutkInfo", {}).get("appTutkUrl")

    async def set_free_feeding_mode(self) -> None:
        """Enable Free Feeding (Smart Feed) mode."""
        _LOGGER.debug(f"Enabling Free Feeding mode for {self.serial}")
        try:
            await self.api.set_feeding_mode(self.serial, "FREE")
            await self.refresh()
        except aiohttp.ClientError as err:
            _LOGGER.error(f"Failed to enable Free Feeding mode for {self.serial}: {err}")
            raise PetLibroAPIError(f"Error enabling Free Feeding mode: {err}")

    async def set_plan_feeding_mode(self) -> None:
        """Switch back to schedule-based (Feeding Plan) mode."""
        _LOGGER.debug(f"Enabling Plan feeding mode for {self.serial}")
        try:
            await self.api.set_feeding_mode(self.serial, "PLAN")
            await self.refresh()
        except aiohttp.ClientError as err:
            _LOGGER.error(f"Failed to enable Plan feeding mode for {self.serial}: {err}")
            raise PetLibroAPIError(f"Error enabling Plan feeding mode: {err}")
