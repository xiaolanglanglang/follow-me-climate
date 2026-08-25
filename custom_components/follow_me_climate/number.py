"""Runtime-tunable number entities for Follow-Me Climate."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ac_target_temp_step
from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_STEP,
    CONF_TARGET,
    DEFAULT_STEP,
    DEFAULT_TARGET,
    DOMAIN,
    MAX_STEP,
    MAX_TARGET,
    MIN_STEP,
    MIN_TARGET,
)
from .controller import FollowMeController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    controller: FollowMeController = hass.data[DOMAIN][entry.entry_id]
    # Slider granularity matches the AC's own setpoint step when it exposes
    # one; the step entity keeps 0.5 so its 0.5-2.0 range stays evenly divisible.
    ac_step = ac_target_temp_step(
        hass, {**entry.data, **entry.options}[CONF_CLIMATE_ENTITY]
    )
    async_add_entities(
        [
            FollowMeNumber(
                hass, controller, entry, CONF_TARGET, "target_temperature",
                native_step=ac_step if ac_step is not None else DEFAULT_STEP,
            ),
            FollowMeNumber(
                hass, controller, entry, CONF_STEP, "step", native_step=DEFAULT_STEP
            ),
        ]
    )


class FollowMeNumber(NumberEntity):
    """A live-tunable control parameter, persisted into entry options."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

    _BOUNDS = {
        CONF_TARGET: (MIN_TARGET, MAX_TARGET, DEFAULT_TARGET),
        CONF_STEP: (MIN_STEP, MAX_STEP, DEFAULT_STEP),
    }
    _CONFIG_ATTR = {CONF_TARGET: "target", CONF_STEP: "step"}

    def __init__(
        self,
        hass: HomeAssistant,
        controller: FollowMeController,
        entry: ConfigEntry,
        key: str,
        translation_key: str,
        native_step: float,
    ) -> None:
        self._hass = hass
        self._controller = controller
        self._entry = entry
        self._key = key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        min_value, max_value, default = self._BOUNDS[key]
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = native_step
        self._attr_native_value = getattr(
            controller.config, self._CONFIG_ATTR[key], default
        )
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_set_native_value(self, value: float) -> None:
        setattr(self._controller.config, self._CONFIG_ATTR[self._key], value)
        self._attr_native_value = value
        self._controller.notify()
        # Persist so the value survives restarts; runtime-only key, so the
        # update listener hot-applies it instead of reloading the entry.
        self._hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, self._key: value}
        )

    @callback
    def _handle_update(self) -> None:
        self._attr_native_value = getattr(
            self._controller.config, self._CONFIG_ATTR[self._key]
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._controller.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._controller.remove_listener(self._handle_update)
