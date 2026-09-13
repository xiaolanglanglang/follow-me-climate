"""Recorder-backed warm start for the control loop.

History removes two cold-start costs. The median filter otherwise boots
with an empty window, so the first ticks act on a single raw reading with
no spike rejection. And the feedforward's instant bias samples whatever
transient the room was in at start-up (stratified air, AC sensor lag),
while a median over recent *runtime* history approaches the steady
person-versus-AC-sensor offset. Both are seeded here when the recorder
has data; without it the loop behaves exactly as before.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.climate import ATTR_CURRENT_TEMPERATURE
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from .const import BIAS_PAIR_WINDOW, BIAS_WINDOW, HVAC_COOL, HVAC_HEAT
from .controller import FollowMeController, median_bias

_LOGGER = logging.getLogger(__name__)


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


async def prime_from_history(
    hass: HomeAssistant,
    controller: FollowMeController,
    climate_entity_id: str,
    sensor_entity_id: str,
) -> None:
    """Seed the median window and the learned feedforward bias."""
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
        bias = median_bias(pairs)
        if bias is None:
            _LOGGER.debug(
                "%s: %d runtime pair(s); not enough for a learned bias",
                controller.name,
                len(pairs),
            )
            return
        controller.learned_bias = round(bias, 2)
        _LOGGER.debug(
            "%s: learned bias %.2f deg from %d runtime pair(s)",
            controller.name,
            controller.learned_bias,
            len(pairs),
        )
    except Exception:  # noqa: BLE001 - history is an accelerant, never a gate
        _LOGGER.exception("%s: history warm-up failed; starting cold", controller.name)
