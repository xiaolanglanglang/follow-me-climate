"""Follow-Me Climate control loop.

Pure control logic with no Home Assistant imports, so it can be unit tested
standalone. The HA layer wires a climate adapter, a sensor reader and a clock
into it (see __init__.py).

Control model: the AC's own room-temperature sensor is treated as unreliable.
The reference sensor (where the person actually stays) is the only trusted
input. A slow closed loop nudges the AC setpoint until the reference
temperature settles on the target:

- cooling: reference too hot  -> lower the AC setpoint
- heating: reference too cold -> raise the AC setpoint

A feedforward pass positions the setpoint once when following starts, using
the (filtered) bias between reference and AC-sensed temperature.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from .const import (
    CONF_DEADBAND,
    CONF_DRY_RUN,
    CONF_FEEDFORWARD,
    CONF_FOLLOW_POWER,
    CONF_INTERVAL,
    CONF_MANUAL_PAUSE,
    CONF_SENSOR_TIMEOUT,
    CONF_STEP,
    CONF_TARGET,
    DEFAULT_DEADBAND,
    DEFAULT_DRY_RUN,
    DEFAULT_FEEDFORWARD,
    DEFAULT_FOLLOW_POWER,
    DEFAULT_INTERVAL,
    DEFAULT_MANUAL_PAUSE,
    DEFAULT_SENSOR_TIMEOUT,
    DEFAULT_STEP,
    DEFAULT_TARGET,
    HVAC_COOL,
    HVAC_HEAT,
    STATUS_ADJUSTING,
    STATUS_IDLE,
    STATUS_INACTIVE,
    STATUS_MANUAL_PAUSE,
    STATUS_SENSOR_LOST,
)

# Sets the tolerance for comparing setpoints (AC resolution is 0.5 degrees).
_EPS = 0.05


@dataclass
class ControllerConfig:
    """Tunable parameters, all adjustable at runtime."""

    target: float = DEFAULT_TARGET
    step: float = DEFAULT_STEP
    interval: float = DEFAULT_INTERVAL  # minutes between adjustments
    deadband: float = DEFAULT_DEADBAND  # +/- degrees around target
    min_sp: float = 16.0
    max_sp: float = 30.0
    sensor_timeout: float = DEFAULT_SENSOR_TIMEOUT  # minutes
    manual_pause: float = DEFAULT_MANUAL_PAUSE  # minutes
    feedforward: bool = DEFAULT_FEEDFORWARD
    dry_run: bool = DEFAULT_DRY_RUN
    follow_power: bool = DEFAULT_FOLLOW_POWER


class ClimateAdapter(Protocol):
    """The climate entity as seen by the controller."""

    @property
    def setpoint(self) -> float | None: ...

    @property
    def current_temperature(self) -> float | None: ...

    @property
    def hvac_mode(self) -> str | None: ...

    async def set_temperature(self, setpoint: float) -> None: ...


# Returns (value, age in seconds); value is None when unavailable.
SensorReader = Callable[[], tuple[float | None, float]]
NowFn = Callable[[], float]


class FollowMeController:
    """State machine driving one climate entity from one reference sensor."""

    def __init__(
        self,
        name: str,
        config: ControllerConfig,
        adapter: ClimateAdapter,
        sensor_reader: SensorReader,
        now_fn: NowFn = time.monotonic,
    ) -> None:
        self.name = name
        self.config = config
        self._adapter = adapter
        self._reader = sensor_reader
        self._now = now_fn

        self.enabled = False
        self.status = STATUS_INACTIVE
        self.status_detail = ""
        self.last_action = ""
        self.last_action_ts: float | None = None
        self.ref_filtered: float | None = None

        # Snapshot of the merged options last applied, used by the update
        # listener to decide between a runtime update and a full reload.
        self.applied_options: dict = {}

        self._readings: deque[float] = deque(maxlen=5)
        self._default_sp: float | None = None
        self._written_sp: float | None = None
        self._manual_until: float | None = None
        self._restored_lost = False
        self._listeners: list[Callable[[], None]] = []

    # -- plumbing ----------------------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback()

    def notify(self) -> None:
        """Let entities refresh after an out-of-band change."""
        self._notify()

    @property
    def applied_setpoint(self) -> float | None:
        return self._written_sp

    @property
    def offset(self) -> float | None:
        """Trim applied to the AC setpoint relative to the target."""
        if self._written_sp is None:
            return None
        return round(self._written_sp - self.config.target, 2)

    def _clamp(self, value: float) -> float:
        return max(self.config.min_sp, min(self.config.max_sp, value))

    def _filtered(self) -> float | None:
        """Median of the last (up to) 3 readings, rejecting single spikes."""
        if not self._readings:
            return None
        vals = sorted(list(self._readings)[-3:])
        n = len(vals)
        if n % 2 == 1:
            return vals[n // 2]
        return (vals[n // 2 - 1] + vals[n // 2]) / 2

    def _record(self, message: str) -> None:
        """Record a setpoint action; this is what the rate gate paces."""
        self.last_action = message
        self.last_action_ts = self._now()

    def _note(self, message: str) -> None:
        """Record an event that must not defer the next adjustment."""
        self.last_action = message

    def update_runtime(self, options: dict) -> None:
        """Apply option changes that do not need an entry reload."""
        cfg = self.config
        cfg.target = float(options.get(CONF_TARGET, cfg.target))
        cfg.step = float(options.get(CONF_STEP, cfg.step))
        cfg.interval = float(options.get(CONF_INTERVAL, cfg.interval))
        cfg.deadband = float(options.get(CONF_DEADBAND, cfg.deadband))
        cfg.sensor_timeout = float(
            options.get(CONF_SENSOR_TIMEOUT, cfg.sensor_timeout)
        )
        cfg.manual_pause = float(options.get(CONF_MANUAL_PAUSE, cfg.manual_pause))
        cfg.feedforward = bool(options.get(CONF_FEEDFORWARD, cfg.feedforward))
        cfg.dry_run = bool(options.get(CONF_DRY_RUN, cfg.dry_run))
        cfg.follow_power = bool(options.get(CONF_FOLLOW_POWER, cfg.follow_power))
        self.applied_options = dict(options)
        self._notify()

    # -- lifecycle ---------------------------------------------------------

    async def enable(self) -> None:
        """Start following. Snapshots the current setpoint as restore default."""
        if self.enabled:
            return
        self.enabled = True
        self._default_sp = self._adapter.setpoint
        self._written_sp = None  # first tick applies feedforward
        self._readings.clear()
        self._manual_until = None
        self._restored_lost = False
        self.last_action_ts = None
        self._note(f"enabled, default setpoint {self._default_sp}")
        self._notify()

    async def disable(self, restore: bool = True) -> None:
        self.enabled = False
        self.status = STATUS_INACTIVE
        self.status_detail = "disabled"
        if (
            restore
            and not self.config.dry_run
            and self._written_sp is not None
            and self._default_sp is not None
            and abs(self._written_sp - self._default_sp) > _EPS
        ):
            await self._adapter.set_temperature(self._default_sp)
            self._record(f"disabled, restored default setpoint {self._default_sp}")
        self._written_sp = None
        self._notify()

    # -- control loop ------------------------------------------------------

    async def tick(self) -> None:
        """One control pass; the platform timer calls this every minute."""
        if not self.enabled:
            return
        cfg = self.config

        # 1. reference sensor health
        value, age = self._reader()
        if value is None or age > cfg.sensor_timeout * 60:
            self.status = STATUS_SENSOR_LOST
            self.status_detail = (
                "unavailable" if value is None else f"stale for {age / 60:.0f} min"
            )
            if not self._restored_lost:
                self._restored_lost = True
                self._written_sp = None  # re-acquisition redoes feedforward
                # Drop the stale window so re-acquisition is not polluted.
                self._readings.clear()
                self.ref_filtered = None
                if (
                    not cfg.dry_run
                    and self._default_sp is not None
                    and self._adapter.setpoint is not None
                    and abs(self._adapter.setpoint - self._default_sp) > _EPS
                ):
                    await self._adapter.set_temperature(self._default_sp)
                    self._record(f"sensor lost, restored {self._default_sp}")
            self._notify()
            return
        self._restored_lost = False
        self._readings.append(value)
        self.ref_filtered = self._filtered()
        ref = self.ref_filtered

        # 2. only act in active heat/cool modes
        hvac = self._adapter.hvac_mode
        if hvac not in (HVAC_COOL, HVAC_HEAT):
            self.status = STATUS_INACTIVE
            self.status_detail = f"hvac mode: {hvac}"
            self._notify()
            return
        direction = 1.0 if hvac == HVAC_COOL else -1.0

        # 3. manual override: someone touched the setpoint elsewhere.
        # While paused (or once it expires) skip the mismatch check, so the
        # pause is not re-triggered every minute before it can elapse.
        cur_sp = self._adapter.setpoint
        if self._manual_until is not None:
            if self._now() < self._manual_until:
                remaining = (self._manual_until - self._now()) / 60
                self.status = STATUS_MANUAL_PAUSE
                self.status_detail = f"resumes in {remaining:.0f} min"
                self._notify()
                return
            self._manual_until = None
            # Adopt the human's setpoint as the new baseline.
            self._written_sp = cur_sp
            self._note(f"resumed from manual setpoint {cur_sp}")
        elif (
            self._written_sp is not None
            and cur_sp is not None
            and abs(cur_sp - self._written_sp) > _EPS
        ):
            self._manual_until = self._now() + cfg.manual_pause * 60
            self.status = STATUS_MANUAL_PAUSE
            self.status_detail = f"setpoint changed externally to {cur_sp}"
            self._note(f"manual override detected: {cur_sp}")
            self._notify()
            return

        # 4. first pass: position via feedforward, then hand over to the loop
        if self._written_sp is None:
            ac_temp = self._adapter.current_temperature
            ff_sp = cur_sp if cur_sp is not None else self._clamp(cfg.target)
            if cfg.feedforward and ac_temp is not None and ref is not None:
                # Person = AC sensor + bias, so drive the AC sensor to
                # target - bias. Same formula for cooling and heating.
                ff_sp = self._clamp(cfg.target - (ref - ac_temp))
            self._written_sp = ff_sp
            if cur_sp is None or abs(ff_sp - cur_sp) > _EPS:
                if not cfg.dry_run:
                    await self._adapter.set_temperature(ff_sp)
                self._record(
                    f"{'would apply' if cfg.dry_run else 'applied'} "
                    f"feedforward setpoint {ff_sp}"
                )
                self.status = STATUS_ADJUSTING
                self.status_detail = (
                    "dry-run, " if cfg.dry_run else ""
                ) + f"feedforward setpoint {ff_sp}"
                self._notify()
                return
            # Nothing to send: hand straight over to the closed loop below.

        # 5. error against the reference sensor, with deadband
        assert ref is not None
        err = direction * (ref - cfg.target)
        if abs(err) <= cfg.deadband:
            self.status = STATUS_IDLE
            self.status_detail = f"reference {ref:.1f}, target {cfg.target:.1f}"
            self._notify()
            return

        # 6. rate gate: gentle cadence keeps the inverter compressor smooth
        if (
            self.last_action_ts is not None
            and self._now() - self.last_action_ts < cfg.interval * 60
        ):
            self.status = STATUS_ADJUSTING
            remaining = (cfg.interval * 60 - (self._now() - self.last_action_ts)) / 60
            self.status_detail = f"next adjustment in {remaining:.0f} min"
            self._notify()
            return

        # 7. step the setpoint toward comfort
        base_sp = self._written_sp
        delta = math.copysign(cfg.step, err) * (-direction)
        new_sp = self._clamp(base_sp + delta)
        if abs(new_sp - base_sp) <= _EPS:
            self.status = STATUS_ADJUSTING
            bound = "min" if delta < 0 else "max"
            self.status_detail = f"holding at {bound} bound {base_sp}"
            self._notify()
            return
        if not cfg.dry_run:
            await self._adapter.set_temperature(new_sp)
        self._written_sp = new_sp
        self._record(
            f"{'would set' if cfg.dry_run else 'set'} {new_sp} "
            f"(reference {ref:.1f}, target {cfg.target:.1f})"
        )
        self.status = STATUS_ADJUSTING
        self.status_detail = (
            ("dry-run, " if cfg.dry_run else "") + f"setpoint {new_sp}"
        )
        self._notify()
