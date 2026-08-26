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
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_DEADBAND,
    CONF_DRY_RUN,
    CONF_FEEDFORWARD,
    CONF_FOLLOW_POWER,
    CONF_INTERVAL,
    CONF_MANUAL_PAUSE,
    CONF_MAX_SP,
    CONF_MIN_SP,
    CONF_NAME,
    CONF_POWER_ENTITY,
    CONF_SENSOR_ENTITY,
    CONF_SENSOR_TIMEOUT,
    CONF_STEP,
    CONF_TARGET,
    DEFAULT_DEADBAND,
    DEFAULT_DRY_RUN,
    DEFAULT_FEEDFORWARD,
    DEFAULT_FOLLOW_POWER,
    DEFAULT_INTERVAL,
    DEFAULT_MANUAL_PAUSE,
    DEFAULT_MAX_SP,
    DEFAULT_MIN_SP,
    DEFAULT_NAME,
    DEFAULT_SENSOR_TIMEOUT,
    DEFAULT_STEP,
    DEFAULT_TARGET,
    DOMAIN,
    HVAC_COOL,
    HVAC_HEAT,
    HVAC_OFF,
    MAX_STEP,
    MIN_STEP,
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


def ac_target_temp_step(hass: HomeAssistant, entity_id: str) -> float | None:
    """The AC's own setpoint granularity, when its integration exposes it."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        value = float(state.attributes.get(ATTR_TARGET_TEMP_STEP))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _build_controller(hass: HomeAssistant, entry: ConfigEntry) -> FollowMeController:
    merged = {**entry.data, **entry.options}
    # Adopt the AC's own setpoint granularity as the control step, unless the
    # user has tuned it (manual changes are persisted into entry.options).
    step = float(merged.get(CONF_STEP, DEFAULT_STEP))
    if CONF_STEP not in entry.options:
        ac_step = ac_target_temp_step(hass, merged[CONF_CLIMATE_ENTITY])
        if ac_step is not None:
            step = max(MIN_STEP, min(MAX_STEP, ac_step))
    config = ControllerConfig(
        target=float(merged.get(CONF_TARGET, DEFAULT_TARGET)),
        step=step,
        interval=float(merged.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
        deadband=float(merged.get(CONF_DEADBAND, DEFAULT_DEADBAND)),
        min_sp=float(merged.get(CONF_MIN_SP, DEFAULT_MIN_SP)),
        max_sp=float(merged.get(CONF_MAX_SP, DEFAULT_MAX_SP)),
        sensor_timeout=float(merged.get(CONF_SENSOR_TIMEOUT, DEFAULT_SENSOR_TIMEOUT)),
        manual_pause=float(merged.get(CONF_MANUAL_PAUSE, DEFAULT_MANUAL_PAUSE)),
        feedforward=bool(merged.get(CONF_FEEDFORWARD, DEFAULT_FEEDFORWARD)),
        dry_run=bool(merged.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)),
        follow_power=bool(merged.get(CONF_FOLLOW_POWER, DEFAULT_FOLLOW_POWER)),
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

    power_reader = None
    power_entity_id = merged.get(CONF_POWER_ENTITY)
    if power_entity_id:

        def read_power() -> tuple[float | None, float]:
            state = hass.states.get(power_entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                return None, 0.0
            try:
                value = float(state.state)
            except ValueError:
                return None, 0.0
            age = (dt_util.utcnow() - state.last_updated).total_seconds()
            return value, max(age, 0.0)

        power_reader = read_power

    controller = FollowMeController(
        name=merged.get(CONF_NAME, DEFAULT_NAME),
        config=config,
        adapter=HAClimateAdapter(hass, merged[CONF_CLIMATE_ENTITY]),
        sensor_reader=read_sensor,
        power_reader=power_reader,
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

    @callback
    def _on_climate_state(event) -> None:
        """Stop following when the AC is switched off (opt-out option)."""
        if not controller.config.follow_power or not controller.enabled:
            return
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        # Only a real cool/heat -> off transition triggers; unavailable is
        # ignored so a flaky link or a powered-off hub cannot spuriously
        # restore setpoints into an entity that cannot accept them.
        if new_state is None or new_state.state != HVAC_OFF:
            return
        if old_state is None or old_state.state not in (HVAC_COOL, HVAC_HEAT):
            return

        async def _stop() -> None:
            try:
                await controller.disable(restore=True)
            except Exception:  # noqa: BLE001 - never kill the listener
                _LOGGER.exception("Follow-Me Climate auto-stop on AC off failed")

        hass.async_create_task(_stop())

    climate_entity_id = {**entry.data, **entry.options}[CONF_CLIMATE_ENTITY]
    entry.async_on_unload(
        async_track_state_change_event(
            hass, [climate_entity_id], _on_climate_state
        )
    )

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
