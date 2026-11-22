from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SaveFamilyConfigEntry
from .const import BACKEND_CONNECT, CONF_BACKEND, LOCATION_STALE_AFTER, ONLINE_THRESHOLD
from .entity import SaveFamilyEntity, async_forget_removed_watches


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaveFamilyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    # Flags only Connect reports; gated on the backend, not on the first poll's values.
    is_connect = entry.data.get(CONF_BACKEND) == BACKEND_CONNECT
    entities_by_did: dict[str, list[SaveFamilyEntity]] = {}
    async_forget_removed_watches(hass, entry, entities_by_did)

    @callback
    def _async_handle_update() -> None:
        new_entities = []
        for did in coordinator.data:
            if did not in entities_by_did:
                es: list[SaveFamilyEntity] = [
                    SaveFamilyLocationStaleSensor(coordinator, did),
                    SaveFamilyOnlineSensor(coordinator, did),
                ]
                if is_connect:
                    es += [
                        SaveFamilyBatteryLowSensor(coordinator, did),
                        SaveFamilyInSafeZoneSensor(coordinator, did),
                    ]
                entities_by_did[did] = es
                new_entities.extend(es)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_handle_update))
    _async_handle_update()


class SaveFamilyLocationStaleSensor(SaveFamilyEntity, BinarySensorEntity):
    _attr_translation_key = "location_stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"{did}_location_stale"

    @property
    def is_on(self) -> bool:
        last_fix = self.snapshot.last_fix
        if last_fix is None:
            return True
        return dt_util.utcnow() - last_fix > LOCATION_STALE_AFTER


class SaveFamilyOnlineSensor(SaveFamilyEntity, BinarySensorEntity):
    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"{did}_online"

    @property
    def is_on(self) -> bool:
        # Connect reports the connection itself; Classic is inferred from the last fix.
        if self.snapshot.connected is not None:
            return self.snapshot.connected
        last_fix = self.snapshot.last_fix
        if last_fix is None:
            return False
        return dt_util.utcnow() - last_fix <= ONLINE_THRESHOLD


class SaveFamilyBatteryLowSensor(SaveFamilyEntity, BinarySensorEntity):
    _attr_translation_key = "battery_low"
    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(self, coordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"{did}_battery_low"

    @property
    def is_on(self) -> bool | None:
        return self.snapshot.battery_low


class SaveFamilyInSafeZoneSensor(SaveFamilyEntity, BinarySensorEntity):
    """Whether the server places the watch inside one of its safe zones."""

    _attr_translation_key = "in_safe_zone"
    _attr_device_class = BinarySensorDeviceClass.PRESENCE
    # Reads "out" on accounts without safe zones.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, did: str) -> None:
        super().__init__(coordinator, did)
        self._attr_unique_id = f"{did}_in_safe_zone"

    @property
    def is_on(self) -> bool | None:
        return self.snapshot.in_safe_zone
