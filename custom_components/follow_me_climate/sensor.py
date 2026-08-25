"""Diagnostic sensors for Follow-Me Climate."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ALL_STATUSES, DOMAIN
from .controller import FollowMeController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    controller: FollowMeController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            FollowMeStatusSensor(controller, entry),
            FollowMeOffsetSensor(controller, entry),
            FollowMeReferenceSensor(controller, entry),
        ]
    )


class FollowMeSensorBase(SensorEntity):
    """Shared listener wiring for controller-driven sensors."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: FollowMeController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._controller.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._controller.remove_listener(self._handle_update)


class FollowMeStatusSensor(FollowMeSensorBase):
    """What the controller is doing right now, and why."""

    _attr_translation_key = "status"
    _attr_options = ALL_STATUSES

    def __init__(self, controller: FollowMeController, entry: ConfigEntry) -> None:
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str | None:
        return self._controller.status

    @property
    def extra_state_attributes(self) -> dict:
        controller = self._controller
        return {
            "detail": controller.status_detail,
            "dry_run": controller.config.dry_run,
            "reference": controller.ref_filtered,
            "applied_setpoint": controller.applied_setpoint,
            "last_action": controller.last_action,
        }


class FollowMeOffsetSensor(FollowMeSensorBase):
    """Trim applied to the AC setpoint relative to the target."""

    _attr_translation_key = "offset"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, controller: FollowMeController, entry: ConfigEntry) -> None:
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_offset"

    @property
    def native_value(self) -> float | None:
        return self._controller.offset


class FollowMeReferenceSensor(FollowMeSensorBase):
    """The filtered reference temperature the controller acts on."""

    _attr_translation_key = "reference"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, controller: FollowMeController, entry: ConfigEntry) -> None:
        super().__init__(controller, entry)
        self._attr_unique_id = f"{entry.entry_id}_reference"

    @property
    def native_value(self) -> float | None:
        return self._controller.ref_filtered
