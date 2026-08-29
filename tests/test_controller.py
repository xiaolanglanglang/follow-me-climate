"""Unit tests for the Follow-Me Climate control loop (no HA imports)."""

import asyncio

from conftest import ControllerConfig, FollowMeController, const

STATUS_ADJUSTING = const.STATUS_ADJUSTING
STATUS_IDLE = const.STATUS_IDLE
STATUS_MANUAL_PAUSE = const.STATUS_MANUAL_PAUSE
STATUS_SENSOR_LOST = const.STATUS_SENSOR_LOST


class FakeAdapter:
    def __init__(self, setpoint=26.0, current_temperature=26.0, hvac_mode="cool"):
        self.setpoint = setpoint
        self.current_temperature = current_temperature
        self.hvac_mode = hvac_mode
        self.writes = []

    async def set_temperature(self, setpoint):
        self.writes.append(setpoint)
        self.setpoint = setpoint


def make_controller(adapter, reader, power_reader=None, **overrides):
    clock = {"t": 1000.0}

    def now():
        return clock["t"]

    controller = FollowMeController(
        name="test",
        config=ControllerConfig(**overrides),
        adapter=adapter,
        sensor_reader=reader,
        power_reader=power_reader,
        now_fn=now,
    )
    return controller, clock


def tick(controller):
    asyncio.run(controller.tick())


def enable(controller):
    asyncio.run(controller.enable())


def test_feedforward_cooling():
    # Person at 28.5, AC believes 26, target 26 -> setpoint 26 - 2.5 = 23.5.
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    controller, _ = make_controller(adapter, lambda: (28.5, 0.0), target=26)
    enable(controller)
    tick(controller)
    assert adapter.writes == [23.5]
    assert controller.status == STATUS_ADJUSTING
    assert controller.offset == -2.5


def test_feedforward_heating():
    # Person at 20, AC believes 24, target 24 -> setpoint 24 + 4 = 28.
    adapter = FakeAdapter(setpoint=24, current_temperature=24, hvac_mode="heat")
    controller, _ = make_controller(adapter, lambda: (20.0, 0.0), target=24)
    enable(controller)
    tick(controller)
    assert adapter.writes == [28]


def test_deadband_idle_no_write():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    controller, _ = make_controller(
        adapter, lambda: (26.2, 0.0), target=26, feedforward=False
    )
    enable(controller)
    tick(controller)
    assert controller.status == STATUS_IDLE
    assert adapter.writes == []


def test_converges_clamps_then_relaxes():
    adapter = FakeAdapter(setpoint=26, current_temperature=27, hvac_mode="cool")
    sensor = {"value": 30.0}
    controller, clock = make_controller(
        adapter, lambda: (sensor["value"], 0.0), target=26, min_sp=25
    )
    enable(controller)
    # Feedforward would be 23, clamped to the configured floor of 25.
    tick(controller)
    assert adapter.writes == [25]
    # Still too hot, but already at the bound: no further writes.
    clock["t"] += 300
    tick(controller)
    assert adapter.writes == [25]
    assert controller.status == STATUS_ADJUSTING
    assert "bound" in controller.status_detail
    # The median filter needs two fresh samples to accept the new level.
    sensor["value"] = 26.2
    clock["t"] += 300
    tick(controller)
    clock["t"] += 300
    tick(controller)
    assert controller.status == STATUS_IDLE
    # Overshoot: person too cold -> relax the setpoint upward.
    sensor["value"] = 25.2
    clock["t"] += 300
    tick(controller)
    clock["t"] += 300
    tick(controller)
    assert adapter.writes[-1] == 25.5


def test_rate_gate_defers_adjustment():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    sensor = {"value": 28.0}
    controller, clock = make_controller(
        adapter, lambda: (sensor["value"], 0.0), target=26, feedforward=False
    )
    enable(controller)
    clock["t"] += 300
    tick(controller)  # first real adjustment
    assert adapter.writes == [25.5]
    sensor["value"] = 28.1
    clock["t"] += 60  # well inside the 4-minute interval
    tick(controller)
    assert adapter.writes == [25.5]
    assert controller.status == STATUS_ADJUSTING
    assert "next adjustment" in controller.status_detail


def test_manual_override_pauses_then_adopts():
    adapter = FakeAdapter(setpoint=26, current_temperature=27, hvac_mode="cool")
    controller, clock = make_controller(
        adapter, lambda: (30.0, 0.0), target=26, manual_pause=30
    )
    enable(controller)
    tick(controller)
    assert adapter.writes  # feedforward applied
    # A human lowers the setpoint via the remote.
    adapter.setpoint = 20.0
    clock["t"] += 300
    tick(controller)
    assert controller.status == STATUS_MANUAL_PAUSE
    writes_before = list(adapter.writes)
    # Still paused a minute later.
    clock["t"] += 60
    tick(controller)
    assert controller.status == STATUS_MANUAL_PAUSE
    assert adapter.writes == writes_before
    # Pause expired: adopt the human's setpoint and continue trimming.
    clock["t"] += 30 * 60
    tick(controller)
    assert adapter.writes[-1] == 19.5
    assert controller.status == STATUS_ADJUSTING


def test_feedforward_snaps_to_ac_grid():
    # 26 - 3.4 = 22.6 is off the AC's 0.5-degree grid; the raw write would
    # be rounded by the device and read back as a phantom manual override.
    adapter = FakeAdapter(setpoint=26, current_temperature=26.4, hvac_mode="cool")
    controller, _ = make_controller(adapter, lambda: (29.8, 0.0), target=26)
    enable(controller)
    tick(controller)
    assert adapter.writes == [22.5]
    assert controller.offset == -3.5


def test_offgrid_readback_does_not_trigger_manual_pause():
    # A device reporting its own (finer) rounding of our write is noise,
    # not a human override: the mismatch sits under half a step.
    adapter = FakeAdapter(setpoint=26, current_temperature=27, hvac_mode="cool")
    controller, clock = make_controller(
        adapter, lambda: (30.0, 0.0), target=26, manual_pause=30
    )
    enable(controller)
    tick(controller)  # feedforward writes 23.0
    assert adapter.writes == [23.0]
    adapter.setpoint = 23.1  # as if the device rounded to 0.1 degrees
    clock["t"] += 60
    tick(controller)
    assert controller.status != STATUS_MANUAL_PAUSE


def test_human_step_change_still_triggers_manual_pause():
    # A genuine remote nudge of one full step stays far over the widened
    # tolerance and must still yield to the human.
    adapter = FakeAdapter(setpoint=26, current_temperature=27, hvac_mode="cool")
    controller, clock = make_controller(
        adapter, lambda: (30.0, 0.0), target=26, manual_pause=30
    )
    enable(controller)
    tick(controller)  # feedforward writes 23.0
    adapter.setpoint = 23.5  # one full step up, unmistakably human
    clock["t"] += 60
    tick(controller)
    assert controller.status == STATUS_MANUAL_PAUSE


def test_sensor_lost_restores_default_and_reacquires():
    adapter = FakeAdapter(setpoint=26, current_temperature=27, hvac_mode="cool")
    sensor = {"value": 30.0, "age": 0.0}
    controller, clock = make_controller(
        adapter, lambda: (sensor["value"], sensor["age"]), target=26
    )
    enable(controller)
    tick(controller)  # feedforward: 26 - 3 = 23
    assert adapter.writes == [23.0]
    # Sensor goes stale beyond the 10-minute timeout.
    sensor["age"] = 900
    clock["t"] += 300
    tick(controller)
    assert controller.status == STATUS_SENSOR_LOST
    assert adapter.writes == [23.0, 26.0]  # restored the enable-time default
    # Repeated ticks while lost do not write again.
    clock["t"] += 300
    tick(controller)
    assert adapter.writes == [23.0, 26.0]
    # Sensor comes back fresh -> feedforward repositions (snapped to the
    # 0.5-degree grid: raw 26 - (26.2 - 27) = 26.8 -> 27.0).
    sensor.update(value=26.2, age=0.0)
    clock["t"] += 300
    tick(controller)
    assert controller.status == STATUS_ADJUSTING
    assert adapter.writes[-1] == 27.0


def test_unavailable_sensor_marks_lost():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    controller, _ = make_controller(
        adapter, lambda: (None, 0.0), target=26, feedforward=False
    )
    enable(controller)
    tick(controller)
    assert controller.status == STATUS_SENSOR_LOST
    assert adapter.writes == []


def test_dry_run_never_writes():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    controller, clock = make_controller(
        adapter, lambda: (29.0, 0.0), target=26, dry_run=True
    )
    enable(controller)
    tick(controller)
    assert controller.status == STATUS_ADJUSTING
    assert controller.applied_setpoint == 23.0
    clock["t"] += 300
    tick(controller)
    assert adapter.writes == []
    # Disabling in dry-run must also not touch the AC.
    asyncio.run(controller.disable())
    assert adapter.writes == []


def test_median_filter_rejects_spike():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    controller, _ = make_controller(adapter, lambda: (26.0, 0.0))
    controller._readings.extend([28.0, 28.2, 40.0])
    assert controller._filtered() == 28.2
    controller._readings.extend([28.1])
    assert controller._filtered() == 28.2


def test_inactive_when_hvac_off_or_fan_only():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="off")
    controller, _ = make_controller(adapter, lambda: (30.0, 0.0), target=26)
    enable(controller)
    tick(controller)
    assert controller.status == "inactive"
    assert adapter.writes == []


def test_disable_restores_default():
    adapter = FakeAdapter(setpoint=26, current_temperature=27, hvac_mode="cool")
    controller, _ = make_controller(adapter, lambda: (30.0, 0.0), target=26)
    enable(controller)
    tick(controller)  # writes 23
    asyncio.run(controller.disable())
    assert adapter.writes[-1] == 26.0
    assert controller.status == "inactive"


def test_follow_power_defaults_on_and_updates_runtime():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    controller, _ = make_controller(adapter, lambda: (26.0, 0.0))
    assert controller.config.follow_power is True
    controller.update_runtime({const.CONF_FOLLOW_POWER: False})
    assert controller.config.follow_power is False
    # A missing key keeps the current value instead of resetting to default.
    controller.update_runtime({})
    assert controller.config.follow_power is False


def test_hvac_constants_match_homeassistant_states():
    # These must equal homeassistant.components.climate.HVACMode verbatim;
    # the suite is HA-free, so the real values are pinned as literals.
    # "cooling"/"heating" once made the control loop and the AC-off
    # auto-stop silently ignore every real climate entity state.
    assert const.HVAC_COOL == "cool"
    assert const.HVAC_HEAT == "heat"
    assert const.HVAC_OFF == "off"


# -- power momentum gate -------------------------------------------------
# Median filtering needs two fresh samples before a new power level shows
# up in power_w; every scenario below ticks twice after level changes.


def test_power_gate_off_without_reader():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    controller, _ = make_controller(
        adapter, lambda: (28.0, 0.0), target=26, feedforward=False
    )
    enable(controller)
    tick(controller)
    assert controller.power_gate == const.POWER_GATE_OFF
    assert controller.power_w is None
    assert adapter.writes == [25.5]


def test_power_lag_window_defers_step():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    sensor = {"value": 28.0}
    power = {"value": 100.0}
    controller, clock = make_controller(
        adapter,
        lambda: (sensor["value"], 0.0),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
        interval=1,
        feedforward=False,
    )
    enable(controller)
    clock["t"] += 60
    tick(controller)
    assert adapter.writes == [25.5]
    assert controller.power_baseline == 100.0
    # Inside the evidence lag window even a huge rise must not be judged.
    power["value"] = 500.0
    for _ in range(2):
        clock["t"] += 60
        tick(controller)
    assert adapter.writes == [25.5]
    assert controller.power_gate == const.POWER_GATE_WAITING
    # Lag elapsed: the rise confirms momentum -> hold instead of stepping.
    clock["t"] += 60
    tick(controller)
    assert adapter.writes == [25.5]
    assert controller.power_gate == const.POWER_GATE_HOLDING
    assert "holding" in controller.status_detail
    # Power recedes past the hysteresis release -> stepping resumes.
    power["value"] = 105.0
    for _ in range(2):
        clock["t"] += 60
        tick(controller)
    assert adapter.writes == [25.5, 25.0]


def test_power_hold_timeout_steps_and_ratchets_baseline():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    sensor = {"value": 28.0}
    power = {"value": 100.0}
    controller, clock = make_controller(
        adapter,
        lambda: (sensor["value"], 0.0),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
        feedforward=False,
    )
    enable(controller)
    clock["t"] += 240
    tick(controller)
    assert adapter.writes == [25.5]
    # Two sub-interval ticks let the median filter pick up the new level,
    # then the decision tick sees the rise and holds.
    power["value"] = 150.0
    clock["t"] += 60
    tick(controller)
    clock["t"] += 60
    tick(controller)
    clock["t"] += 120
    tick(controller)
    assert controller.power_gate == const.POWER_GATE_HOLDING
    assert adapter.writes == [25.5]
    # The hold defers stepping only up to its timeout, then steps and
    # re-anchors the baseline at the current draw.
    clock["t"] += 4 * 240
    tick(controller)
    assert adapter.writes == [25.5, 25.0]
    assert controller.power_baseline == 150.0
    # The ratcheted baseline means a flat high draw no longer blocks.
    clock["t"] += 240
    tick(controller)
    assert adapter.writes == [25.5, 25.0, 24.5]


def test_power_unavailable_falls_back_to_temperature_loop():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    sensor = {"value": 28.0}
    power = {"value": None}
    controller, clock = make_controller(
        adapter,
        lambda: (sensor["value"], 0.0),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
        interval=1,
        feedforward=False,
    )
    enable(controller)
    for _ in range(2):
        clock["t"] += 60
        tick(controller)
    assert controller.power_gate == const.POWER_GATE_UNAVAILABLE
    # No anchor to compare against -> the gate never blocks.
    assert adapter.writes == [25.5, 25.0]


def test_power_loss_mid_hold_releases():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    sensor = {"value": 28.0}
    power = {"value": 100.0}
    controller, clock = make_controller(
        adapter,
        lambda: (sensor["value"], 0.0),
        power_reader=lambda: (power["value"], power.get("age", 0.0)),
        target=26,
        interval=1,
        feedforward=False,
    )
    enable(controller)
    clock["t"] += 60
    tick(controller)  # step, anchored at 100 W
    power["value"] = 500.0
    for _ in range(4):
        clock["t"] += 60
        tick(controller)  # two ticks filter, then wait + hold
    assert controller.power_gate == const.POWER_GATE_HOLDING
    # The meter dies mid-hold: the same tick releases and steps.
    power.update(value=None, age=0.0)
    clock["t"] += 60
    tick(controller)
    assert controller.power_gate == const.POWER_GATE_UNAVAILABLE
    assert adapter.writes == [25.5, 25.0]


def test_power_gate_inert_in_dry_run():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    sensor = {"value": 28.0}
    power = {"value": 500.0}
    controller, clock = make_controller(
        adapter,
        lambda: (sensor["value"], 0.0),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
        interval=1,
        feedforward=False,
        dry_run=True,
    )
    enable(controller)
    for _ in range(3):
        clock["t"] += 60
        tick(controller)
    assert adapter.writes == []
    # Would-steps keep coming at cadence despite the elevated draw.
    assert controller.applied_setpoint == 24.5
    assert controller.power_gate != const.POWER_GATE_HOLDING


def test_feedforward_arms_power_gate():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    power = {"value": 100.0}
    controller, clock = make_controller(
        adapter,
        lambda: (28.5, 0.0),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
    )
    enable(controller)
    tick(controller)  # feedforward write
    assert adapter.writes == [23.5]
    assert controller.power_baseline == 100.0


def test_gate_waiting_from_the_write_itself():
    # After a write the rate gate hides _power_blocks_step for a whole
    # interval; the gate must already read waiting, not the stale
    # constructor "unavailable" seen for minutes after a reload.
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    power = {"value": 100.0}
    controller, _ = make_controller(
        adapter,
        lambda: (28.5, 0.0),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
    )
    enable(controller)
    tick(controller)  # feedforward write, anchored
    assert controller.power_gate == const.POWER_GATE_WAITING


def test_gate_open_with_fresh_meter_and_nothing_anchored():
    # A live meter with no write to judge reads open immediately, even on
    # tick paths that return before the gate is ever evaluated.
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="off")
    power = {"value": 100.0}
    controller, _ = make_controller(
        adapter,
        lambda: (30.0, 0.0),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
    )
    enable(controller)
    tick(controller)  # inactive: returns before _power_blocks_step
    assert controller.status == "inactive"
    assert controller.power_gate == const.POWER_GATE_OPEN


def test_manual_override_rearms_power_gate():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    sensor = {"value": 28.0}
    power = {"value": 100.0}
    controller, clock = make_controller(
        adapter,
        lambda: (sensor["value"], 0.0),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
        interval=1,
        feedforward=False,
        manual_pause=30,
    )
    enable(controller)
    clock["t"] += 60
    tick(controller)  # step to 25.5, anchored at 100 W
    # A human takes over; the hold state must not survive the adoption.
    adapter.setpoint = 20.0
    clock["t"] += 60
    tick(controller)
    assert controller.status == STATUS_MANUAL_PAUSE
    # While paused the draw rises; the filter picks it up.
    power["value"] = 500.0
    clock["t"] += 60
    tick(controller)
    clock["t"] += 60
    tick(controller)
    clock["t"] += 30 * 60 - 120  # pause expires
    tick(controller)  # adopts 20.0, resets tracking, steps to 19.5
    assert adapter.writes[-1] == 19.5
    # The step re-anchored the baseline at the current draw.
    assert controller.power_baseline == 500.0


def test_sensor_lost_clears_power_tracking():
    adapter = FakeAdapter(setpoint=26, current_temperature=26, hvac_mode="cool")
    sensor = {"value": 28.0, "age": 0.0}
    power = {"value": 100.0}
    controller, clock = make_controller(
        adapter,
        lambda: (sensor["value"], sensor["age"]),
        power_reader=lambda: (power["value"], 0.0),
        target=26,
        interval=1,
        feedforward=False,
    )
    enable(controller)
    clock["t"] += 60
    tick(controller)
    assert controller.power_baseline == 100.0
    sensor["age"] = 900
    clock["t"] += 60
    tick(controller)
    assert controller.status == STATUS_SENSOR_LOST
    assert controller.power_baseline is None
