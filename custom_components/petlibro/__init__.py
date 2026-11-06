import logging

from datetime import timedelta  # For managing the update interval
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed  # For coordinator and update handling
from .devices import Device
from .devices.feeders.feeder import Feeder
from .devices.feeders.air_smart_feeder import AirSmartFeeder
from .devices.feeders.granary_smart_feeder import GranarySmartFeeder
from .devices.feeders.granary_smart_camera_feeder import GranarySmartCameraFeeder
from .devices.feeders.one_rfid_smart_feeder import OneRFIDSmartFeeder
from .devices.feeders.polar_wet_food_feeder import PolarWetFoodFeeder
from .devices.feeders.space_smart_feeder import SpaceSmartFeeder
from .devices.fountains.dockstream_smart_fountain import DockstreamSmartFountain
from .devices.fountains.dockstream_smart_rfid_fountain import DockstreamSmartRFIDFountain
from .devices.fountains.dockstream_2_smart_cordless_fountain import Dockstream2SmartCordlessFountain
from .devices.fountains.dockstream_2_smart_fountain import Dockstream2SmartFountain
from .devices.litterboxes.litter_box import LitterBox
from .devices.litterboxes.luma_smart_litter_box import LumaSmartLitterBox
from .const import DOMAIN, PLATFORMS, UPDATE_INTERVAL_SECONDS, IntegrationSetting  # Assuming UPDATE_INTERVAL_SECONDS is defined in const
from .hub import PetLibroHub
from .push import PetLibroPush
from .push.certificates import CertificateStore
from .security import AccountCredentials, migrate_entry_data
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


# Define the platforms for each device type
PLATFORMS_BY_TYPE = {
    Feeder: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    AirSmartFeeder: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    GranarySmartFeeder: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    GranarySmartCameraFeeder: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    OneRFIDSmartFeeder: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    PolarWetFoodFeeder: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    SpaceSmartFeeder: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    DockstreamSmartFountain: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    DockstreamSmartRFIDFountain: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    Dockstream2SmartCordlessFountain: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    Dockstream2SmartFountain: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
    LumaSmartLitterBox: (
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.SWITCH,
        Platform.BUTTON,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.TEXT,
        Platform.UPDATE
    ),
}


def get_platforms_for_devices(devices: list[Device]) -> set[Platform]:
    """Get platforms for devices."""
    return {
        platform
        for device in devices
        for device_type, platforms in PLATFORMS_BY_TYPE.items()
        if isinstance(device, device_type)
        for platform in platforms
    }


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries.

    1.1 → 1.2: the stored password is replaced by its digest (the API only needs the digest).
    """
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        hass.config_entries.async_update_entry(entry, data=migrate_entry_data(entry.data), minor_version=2)
        _LOGGER.info("PETLIBRO: stored password replaced by its digest (config entry 1.2)")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up platform from a ConfigEntry."""
    # Ensure the e-mail and the password digest exist
    try:
        AccountCredentials.from_entry_data(entry.data)
    except ValueError:
        _LOGGER.error("Email or password is missing in the configuration entry. Cannot proceed.")
        return False

    # Initialize PetLibroHub
    try:
        hub = PetLibroHub(hass, entry)

        # Store the hub in hass.data
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

        # Initialize Helpers
        await hub._initialize_helpers()

        # Load member only once here
        await hub.load_member()

        # Load devices only once here
        await hub.load_devices()

        # Load pets only once here
        await hub.load_pets()

        # Start the coordinator for periodic updates
        await hub.coordinator.async_config_entry_first_refresh()

        # Forward entry setups for each platform
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        await async_setup_services(hass)

        _async_start_live_updates(hass, entry, hub)

        _LOGGER.info("Successfully set up the PetLibro integration")
        return True

    except Exception as err:
        _LOGGER.error(f"Failed to set up PetLibro integration: {err}", exc_info=True)
        return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Get the hub from Home Assistant's domain data
    hub = hass.data[DOMAIN].pop(entry.entry_id, None)

    if hub is None:
        _LOGGER.warning(f"PetLibro hub for entry {entry.entry_id} not found.")
        return False

    # Unload platforms associated with the entry
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        _LOGGER.info("Successfully unloaded the PetLibro entry")
        await hub.async_unload()  # If you have any cleanup to do in the hub
        await async_unload_services(hass)
    else:
        _LOGGER.error("Failed to unload the PetLibro entry")

    return unload_ok


def _async_start_live_updates(hass: HomeAssistant, entry: ConfigEntry, hub: PetLibroHub) -> None:
    """Start live updates (PETLIBRO's push channel) unless they are turned off."""
    setting = IntegrationSetting.ENABLE_LIVE_UPDATES
    if not entry.options.get(setting, setting.default):
        _LOGGER.debug("PETLIBRO live updates are turned off")
        return
    if (member_id := hub.member.member_id) is None:
        _LOGGER.warning("PETLIBRO live updates unavailable: the account's member id is unknown")
        return
    hub.push = PetLibroPush(hass, entry, hub, member_id)
    hub.push.async_start()


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored live-update certificate together with the entry."""
    await CertificateStore(hass, entry.entry_id).async_remove()


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device_entry: DeviceEntry) -> bool:
    """Remove a config entry from a device."""
    hub = hass.data[DOMAIN].get(entry.entry_id)
    
    if not hub:
        _LOGGER.warning("No hub found for this entry during device removal.")
        return False

    # Match the serial number with devices in the hub
    return not any(
        identifier
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN
        for device in hub.devices.values()
        if device.serial == identifier[1]
    )
