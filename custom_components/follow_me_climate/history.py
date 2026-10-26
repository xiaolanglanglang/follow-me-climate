"""Recorder-backed warm start for the control loop.

History removes two cold-start costs. The median filter otherwise boots
with an empty window, so the first ticks act on a single raw reading with
no spike rejection. And the feedforward's instant bias samples whatever
transient the room was in at start-up (stratified air, AC sensor lag),
while a median over recent *runtime* history approaches the steady
person-versus-AC-sensor offset. Both are seeded here when the recorder
has data; without it the loop behaves exactly as before.

It also seeds the settled dropout — the gap between the reference
temperature and the setpoint that was holding it — from blocks where the
setpoint had not moved for a while. That is the value the feedforward
positions on, and it is the only usable one on units whose reported
temperature tracks their own setpoint.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from .const import (
    BIAS_PAIR_WINDOW,
    BIAS_WINDOW,
    DROPOUT_PAIR_WINDOW,
    DROPOUT_SETTLE_SECONDS,
    HVAC_ACTIONS_WORKING,
    HVAC_COOL,
    HVAC_HEAT,
)
from .controller import FollowMeController, median_bias, median_dropout

_LOGGER = logging.getLogger(__name__)

_SP_EPS = 0.05


async def _fetch_states(
    hass: HomeAssistant, climate_entity_id: str, sensor_entity_id: str
) -> dict:
    def run() -> dict:
        # Keyword-only on purpose: entity_ids and end_time swapped
        # positions between supported HA versions.
        return get_significant_states(
            hass,
            start_time=dt_util.utcnow() - timedelta(seconds=BIAS_WINDOW),
            entity_ids=[climate_entity_id, sensor_entity_id],
            significant_changes_only=False,
        )

    return await get_instance(hass).async_add_executor_job(run)


def _ts(state) -> float:
    ts = getattr(state, "last_updated_ts", None)
    if ts is not None:
        return ts
    return dt_util.as_timestamp(state.last_updated)


def _numeric(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _runtime_pairs(climate_states: list, ref_states: list) -> list[tuple[float, float]]:
    """Pair each active-mode climate reading with the nearest reference one.

    Off-periods are skipped on purpose: a room that sat unmixed overnight
    has a different (useless) bias distribution than one under active
    heating or cooling.
    """
    pairs: list[tuple[float, float]] = []
    if not ref_states or not climate_states:
        return pairs
    i = 0
    for climate in climate_states:
        if climate.state not in (HVAC_COOL, HVAC_HEAT):
            continue
        ac = _numeric(climate.attributes.get(ATTR_CURRENT_TEMPERATURE))
        if ac is None:
            continue
        climate_ts = _ts(climate)
        while i + 1 < len(ref_states) and abs(_ts(ref_states[i + 1]) - climate_ts) <= abs(
            _ts(ref_states[i]) - climate_ts
        ):
            i += 1
        ref = ref_states[i]
        if abs(_ts(ref) - climate_ts) <= BIAS_PAIR_WINDOW:
            value = _numeric(ref.state)
            if value is not None:
                pairs.append((value, ac))
    return pairs


def _settled_dropout_pairs(
    climate_states: list, ref_states: list, target: float, deadband: float
) -> list[tuple[float, float]]:
    """Pair settled comfort-time setpoints with the nearest reference reading.

    Only blocks where the setpoint itself had been unchanged for
    DROPOUT_SETTLE_SECONDS count, and only readings already inside the
    deadband of the target: a setpoint the loop is still walking — or one
    the room is nowhere near — says nothing about where the room settles.
    The gap is only transferable at comfort, which is precisely the
    question the feedforward asks ("where does the setpoint have to sit to
    hold the target?").
    """
    pairs: list[tuple[float, float]] = []
    if not ref_states or not climate_states:
        return pairs
    last_sp: float | None = None
    last_sp_ts = 0.0
    i = 0
    for climate in climate_states:
        sp = _numeric(climate.attributes.get(ATTR_TEMPERATURE))
        if sp is None:
            continue
        climate_ts = _ts(climate)
        if last_sp is None or abs(sp - last_sp) > _SP_EPS:
            # A new block starts: the setpoint moved, so the room has not
            # caught up with it yet.
            last_sp = sp
            last_sp_ts = climate_ts
            continue
        if climate.state not in (HVAC_COOL, HVAC_HEAT):
            continue
        if climate_ts - last_sp_ts < DROPOUT_SETTLE_SECONDS:
            continue
        action = climate.attributes.get(ATTR_HVAC_ACTION)
        if action is not None and action not in HVAC_ACTIONS_WORKING:
            continue
        while i + 1 < len(ref_states) and abs(
            _ts(ref_states[i + 1]) - climate_ts
        ) <= abs(_ts(ref_states[i]) - climate_ts):
            i += 1
        ref = ref_states[i]
        if abs(_ts(ref) - climate_ts) > DROPOUT_PAIR_WINDOW:
            continue
        value = _numeric(ref.state)
        if value is None or abs(value - target) > deadband:
            continue
        pairs.append((value, sp))
    return pairs


async def prime_from_history(
    hass: HomeAssistant,
    controller: FollowMeController,
    climate_entity_id: str,
    sensor_entity_id: str,
) -> None:
    """Seed the median window, the learned bias and the settled dropout."""
    if "recorder" not in hass.config.components:
        _LOGGER.debug("%s: no recorder; starting cold", controller.name)
        return
    try:
        states_by_entity = await _fetch_states(hass, climate_entity_id, sensor_entity_id)
        ref_states = sorted(states_by_entity.get(sensor_entity_id, []), key=_ts)
        climate_states = sorted(states_by_entity.get(climate_entity_id, []), key=_ts)

        now_ts = dt_util.as_timestamp(dt_util.utcnow())
        fresh = [
            value
            for state in ref_states
            if now_ts - _ts(state) <= controller.config.sensor_timeout * 60
            and (value := _numeric(state.state)) is not None
        ]
        if fresh:
            controller.prime_readings(fresh[-3:])
            _LOGGER.debug(
                "%s: median window primed with %d reading(s)",
                controller.name,
                min(len(fresh), 3),
            )

        pairs = _runtime_pairs(climate_states, ref_states)
        controller.bias_samples = len(pairs)
        bias = median_bias(pairs)
        if bias is None:
            _LOGGER.debug(
                "%s: %d runtime pair(s); not enough for a learned bias",
                controller.name,
                len(pairs),
            )
        else:
            controller.learned_bias = round(bias, 2)
            _LOGGER.debug(
                "%s: learned bias %.2f deg from %d runtime pair(s)",
                controller.name,
                controller.learned_bias,
                len(pairs),
            )

        settled = _settled_dropout_pairs(
            climate_states,
            ref_states,
            controller.config.target,
            controller.config.deadband,
        )
        controller.dropout_samples = len(settled)
        dropout = median_dropout(settled)
        if dropout is None:
            _LOGGER.debug(
                "%s: %d settled pair(s); not enough for a learned dropout",
                controller.name,
                len(settled),
            )
        else:
            controller.learned_dropout = round(dropout, 2)
            _LOGGER.debug(
                "%s: learned dropout %.2f deg from %d settled pair(s)",
                controller.name,
                controller.learned_dropout,
                len(settled),
            )
    except Exception:  # noqa: BLE001 - history is an accelerant, never a gate
        _LOGGER.exception("%s: history warm-up failed; starting cold", controller.name)
