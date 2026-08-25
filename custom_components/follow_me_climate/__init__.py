"""The Follow-Me Climate integration.

Keeps the room temperature at the reference sensor (where the person stays),
not at the AC's built-in sensor, by gently trimming the AC setpoint.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import homeassistant.util.dt as dt_util
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_DEADBAND,
    CONF_DRY_RUN,
    CONF_FEEDFORWARD,
    CONF_INTERVAL,
    CONF_MANUAL_PAUSE,
    CONF_MAX_SP,
    CONF_MIN_SP,
    CONF_NAME,
    CONF_SENSOR_ENTITY,
    CONF_SENSOR_TIMEOUT,
    CONF_STEP,
    CONF_TARGET,
    DEFAULT_DEADBAND,
    DEFAULT_DRY_RUN,
    DEFAULT_FEEDFORWARD,
    DEFAULT_INTERVAL,
    DEFAULT_MANUAL_PAUSE,
    DEFAULT_MAX_SP,
    DEFAULT_MIN_SP,
    DEFAULT_NAME,
    DEFAULT_SENSOR_TIMEOUT,
    DEFAULT_STEP,
    DEFAULT_TARGET,
    DOMAIN,
    STRUCTURAL_KEYS,
)
from .controller import ControllerConfig, FollowMeController

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]
# The control loop is event-driven; the base timer only refreshes state and
# enforces the adjustable cadence (controller rate-gates actual writes).
POLL_INTERVAL = timedelta(minutes=1)


class HAClimateAdapter:
    """Climate entity adapter backed by HA states and services."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self._hass = hass
        self._entity_id = entity_id

    @property
    def _state(self):
        return self._hass.states.get(self._entity_id)

    @property
    def setpoint(self) -> float | None:
        state = self._state
        if state is None:
            return None
        return state.attributes.get(ATTR_TEMPERATURE)

    @property
    def current_temperature(self) -> float | None:
        state = self._state
        if state is None:
            return None
        return state.attributes.get(ATTR_CURRENT_TEMPERATURE)

    @property
    def hvac_mode(self) -> str | None:
        state = self._state
        return state.state if state else None

    async def set_temperature(self, setpoint: float) -> None:
        await self._hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: self._entity_id, ATTR_TEMPERATURE: setpoint},
            blocking=True,
        )


def _build_controller(hass: HomeAssistant, entry: ConfigEntry) -> FollowMeController:
    merged = {**entry.data, **entry.options}
    config = ControllerConfig(
        target=float(merged.get(CONF_TARGET, DEFAULT_TARGET)),
        step=float(merged.get(CONF_STEP, DEFAULT_STEP)),
        interval=float(merged.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
        deadband=float(merged.get(CONF_DEADBAND, DEFAULT_DEADBAND)),
        min_sp=float(merged.get(CONF_MIN_SP, DEFAULT_MIN_SP)),
        max_sp=float(merged.get(CONF_MAX_SP, DEFAULT_MAX_SP)),
        sensor_timeout=float(merged.get(CONF_SENSOR_TIMEOUT, DEFAULT_SENSOR_TIMEOUT)),
        manual_pause=float(merged.get(CONF_MANUAL_PAUSE, DEFAULT_MANUAL_PAUSE)),
        feedforward=bool(merged.get(CONF_FEEDFORWARD, DEFAULT_FEEDFORWARD)),
        dry_run=bool(merged.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)),
    )
    sensor_entity_id = merged[CONF_SENSOR_ENTITY]

    def read_sensor() -> tuple[float | None, float]:
        state = hass.states.get(sensor_entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None, 0.0
        try:
            value = float(state.state)
        except ValueError:
            return None, 0.0
        age = (dt_util.utcnow() - state.last_updated).total_seconds()
        return value, max(age, 0.0)

    controller = FollowMeController(
        name=merged.get(CONF_NAME, DEFAULT_NAME),
        config=config,
        adapter=HAClimateAdapter(hass, merged[CONF_CLIMATE_ENTITY]),
        sensor_reader=read_sensor,
    )
    controller.applied_options = merged
    return controller


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Follow-Me Climate from a config entry."""
    controller = _build_controller(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller

    async def _tick(now=None) -> None:
        try:
            await controller.tick()
        except Exception:  # noqa: BLE001 - never kill the timer
            _LOGGER.exception("Follow-Me Climate tick failed")

    entry.async_on_unload(async_track_time_interval(hass, _tick, POLL_INTERVAL))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Start following right away with one immediate pass.
    try:
        await controller.enable()
        await _tick()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Follow-Me Climate initial pass failed")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    controller: FollowMeController | None = (
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    )
    if controller is not None:
        try:
            await controller.disable(restore=True)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Follow-Me Climate restore on unload failed")
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on structural changes, hot-apply runtime-only changes."""
    controller: FollowMeController | None = (
        hass.data.get(DOMAIN, {}).get(entry.entry_id)
    )
    merged = {**entry.data, **entry.options}
    if controller is None or any(
        merged.get(key) != controller.applied_options.get(key)
        for key in STRUCTURAL_KEYS
    ):
        await hass.config_entries.async_reload(entry)
        return
    controller.update_runtime(merged)
