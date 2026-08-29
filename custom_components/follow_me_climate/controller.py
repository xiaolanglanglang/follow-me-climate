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

Optional power gating: a wattmeter reader turns the loop's blind stepping
into evidence-paced stepping. Power evidence lags a write (inverter ramp-up
plus meter aggregation), so it is only judged after a lag window; a rising
draw means the last write is still doing its job, and further steps wait.
The gate only ever defers stepping — it never replaces the reference loop,
and it disarms itself whenever the power signal is missing or stale.
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
    POWER_GATE_HOLDING,
    POWER_GATE_OFF,
    POWER_GATE_OPEN,
    POWER_GATE_UNAVAILABLE,
    POWER_GATE_WAITING,
    POWER_HOLD_TIMEOUT,
    POWER_RESPONSE_LAG,
    POWER_RISE_MIN_W,
    POWER_RISE_RATIO,
    POWER_STALE_TIMEOUT,
    STATUS_ADJUSTING,
    STATUS_IDLE,
    STATUS_INACTIVE,
    STATUS_MANUAL_PAUSE,
    STATUS_SENSOR_LOST,
)

# Tolerance for comparing our own numbers (writes, snapshots). The
# manual-override check is wider — half a control step — so a device
# rounding our write to its own resolution cannot read back as a human
# touch (see _override_tolerance).
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
# The power reader shares this contract (value in watts).
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
        power_reader: SensorReader | None = None,
        now_fn: NowFn = time.monotonic,
    ) -> None:
        self.name = name
        self.config = config
        self._adapter = adapter
        self._reader = sensor_reader
        self._power_reader = power_reader
        self._now = now_fn

        self.enabled = False
        self.status = STATUS_INACTIVE
        self.status_detail = ""
        self.last_action = ""
        self.last_action_ts: float | None = None
        self.ref_filtered: float | None = None

        # Power gating (inert without a power reader).
        self.power_w: float | None = None
        self.power_gate = (
            POWER_GATE_OFF if power_reader is None else POWER_GATE_UNAVAILABLE
        )

        # Snapshot of the merged options last applied, used by the update
        # listener to decide between a runtime update and a full reload.
        self.applied_options: dict = {}

        self._readings: deque[float] = deque(maxlen=5)
        self._power_readings: deque[float] = deque(maxlen=5)
        self._power_baseline: float | None = None
        self._power_write_ts: float | None = None
        self._momentum_since: float | None = None
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

    @property
    def power_baseline(self) -> float | None:
        """Wattage snapshotted at the moment of the last setpoint write."""
        return self._power_baseline

    def _clamp(self, value: float) -> float:
        return max(self.config.min_sp, min(self.config.max_sp, value))

    def _snap(self, value: float) -> float:
        """Snap a would-be write onto the control step grid.

        The AC rounds any off-grid setpoint to its own resolution; the
        rounded read-back would otherwise look like a manual override.
        """
        grid = round(value / self.config.step) * self.config.step
        return self._clamp(round(grid, 6))

    def _override_tolerance(self) -> float:
        """A mismatch under half a step is device rounding, not a human."""
        return max(_EPS, self.config.step / 2)

    @staticmethod
    def _median(readings: deque[float]) -> float | None:
        if not readings:
            return None
        vals = sorted(list(readings)[-3:])
        n = len(vals)
        if n % 2 == 1:
            return vals[n // 2]
        return (vals[n // 2 - 1] + vals[n // 2]) / 2

    def _filtered(self) -> float | None:
        """Median of the last (up to) 3 readings, rejecting single spikes."""
        return self._median(self._readings)

    # -- power gating ------------------------------------------------------

    def _reset_power_tracking(self) -> None:
        """Forget write-anchored power state (enable, overrides, sensor loss)."""
        self._power_baseline = None
        self._power_write_ts = None
        self._momentum_since = None

    def _sample_power(self) -> None:
        """Advance the power filter once per tick."""
        if self._power_reader is None:
            return
        value, age = self._power_reader()
        if value is None or age > POWER_STALE_TIMEOUT:
            # Evidence gone: release any hold and fall back to the pure
            # temperature loop rather than trusting a dead meter.
            self.power_w = None
            self.power_gate = POWER_GATE_UNAVAILABLE
            self._power_readings.clear()
            self._reset_power_tracking()
            return
        self._power_readings.append(value)
        self.power_w = self._median(self._power_readings)
        # Fresh evidence but nothing anchored: no write is being judged, so
        # the gate is open. Without this the constructor's "unavailable"
        # lingers through windows where _power_blocks_step never runs
        # (rate gate after a write, hvac inactive, manual pause).
        if self._power_write_ts is None or self._power_baseline is None:
            self.power_gate = POWER_GATE_OPEN

    def _arm_power_gate(self) -> None:
        """Anchor power tracking to a just-sent setpoint write."""
        if self._power_reader is None or self.config.dry_run:
            return
        self._power_write_ts = self._now()
        self._power_baseline = self.power_w
        self._momentum_since = None
        # Evidence will not be judged until the lag window elapses; show
        # waiting from the write itself instead of the previous gate state.
        # A dead meter at write time has nothing to wait for and keeps the
        # "unavailable" the sampler already flagged.
        if self.power_w is not None:
            self.power_gate = POWER_GATE_WAITING

    def _power_blocks_step(self) -> bool:
        """Momentum gate: True while the draw says the last write is still
        ramping the room toward the target and further steps should wait.

        Power lags a write (inverter ramp plus meter aggregation), so nothing
        is judged inside the lag window; a sustained hold is capped so a
        lying meter cannot starve the temperature loop.
        """
        if self._power_reader is None or self.config.dry_run:
            return False
        now = self._now()
        power = self.power_w
        if power is None:
            return False  # unavailable; _sample_power already flagged it
        baseline = self._power_baseline
        write_ts = self._power_write_ts
        if baseline is None or write_ts is None:
            # Nothing anchored yet; the next write arms the gate.
            self.power_gate = POWER_GATE_OPEN
            return False
        if now - write_ts < POWER_RESPONSE_LAG:
            self.power_gate = POWER_GATE_WAITING
            return True
        delta = power - baseline
        rise = max(POWER_RISE_RATIO * baseline, POWER_RISE_MIN_W)
        confirmed = delta >= rise
        # Hysteresis: a hold persists until the rise decays to half.
        in_band = delta >= rise / 2
        if self._momentum_since is not None and (confirmed or in_band):
            held_for = now - self._momentum_since
            if held_for < POWER_HOLD_TIMEOUT:
                self.power_gate = POWER_GATE_HOLDING
                return True
            # Held out: trust the temperature loop, and ratchet the baseline
            # to the current draw so the gate re-arms only on a further rise.
            self._power_baseline = power
        elif confirmed:
            self._momentum_since = now
            self.power_gate = POWER_GATE_HOLDING
            return True
        self._momentum_since = None
        self.power_gate = POWER_GATE_OPEN
        return False

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
        self._power_readings.clear()
        self._reset_power_tracking()
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
        self._reset_power_tracking()
        self._notify()

    # -- control loop ------------------------------------------------------

    async def tick(self) -> None:
        """One control pass; the platform timer calls this every minute."""
        if not self.enabled:
            return
        cfg = self.config
        self._sample_power()

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
                self._reset_power_tracking()
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
            # Their write happened out of band; re-anchor power to ours.
            self._reset_power_tracking()
            self._note(f"resumed from manual setpoint {cur_sp}")
        elif (
            # Dry-run never writes, so a mismatch against would-be setpoints
            # is not a human override; pausing on it would stall the trial.
            not cfg.dry_run
            and self._written_sp is not None
            and cur_sp is not None
            and abs(cur_sp - self._written_sp) > self._override_tolerance()
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
            ff_sp = cur_sp if cur_sp is not None else self._snap(cfg.target)
            if cfg.feedforward and ac_temp is not None and ref is not None:
                # Person = AC sensor + bias, so drive the AC sensor to
                # target - bias. Same formula for cooling and heating.
                ff_sp = self._snap(cfg.target - (ref - ac_temp))
            self._written_sp = ff_sp
            if cur_sp is None or abs(ff_sp - cur_sp) > _EPS:
                if not cfg.dry_run:
                    await self._adapter.set_temperature(ff_sp)
                    self._arm_power_gate()
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
            # Comfort reached: any momentum tracking is moot until the next
            # write re-arms it.
            if self._power_reader is not None:
                self._momentum_since = None
                self.power_gate = POWER_GATE_OPEN
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

        # 6.5 power momentum gate: a rising draw means the last write is
        # still doing its job; let the reference temperature catch up first.
        if self._power_blocks_step():
            self.status = STATUS_ADJUSTING
            if self.power_gate == POWER_GATE_WAITING:
                self.status_detail = "awaiting power response"
            else:
                assert self._power_baseline is not None and self.power_w is not None
                self.status_detail = (
                    f"power {self.power_w - self._power_baseline:+.0f} W "
                    "vs baseline, holding"
                )
            self._notify()
            return

        # 7. step the setpoint toward comfort
        base_sp = self._written_sp
        delta = math.copysign(cfg.step, err) * (-direction)
        new_sp = self._snap(base_sp + delta)
        if abs(new_sp - base_sp) <= _EPS:
            self.status = STATUS_ADJUSTING
            bound = "min" if delta < 0 else "max"
            self.status_detail = f"holding at {bound} bound {base_sp}"
            self._notify()
            return
        if not cfg.dry_run:
            await self._adapter.set_temperature(new_sp)
            self._arm_power_gate()
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
