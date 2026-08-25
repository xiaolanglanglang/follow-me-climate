"""Enable/disable switch for Follow-Me Climate."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .controller import FollowMeController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    controller: FollowMeController = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FollowMeSwitch(controller, entry)])


class FollowMeSwitch(SwitchEntity):
    """Master switch: turning it off restores the default setpoint."""

    _attr_has_entity_name = True
    _attr_translation_key = "enable"

    def __init__(self, controller: FollowMeController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._attr_unique_id = f"{entry.entry_id}_enable"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    @property
    def is_on(self) -> bool:
        return self._controller.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._controller.enable()
        await self._controller.tick()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._controller.disable(restore=True)

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._controller.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._controller.remove_listener(self._handle_update)
