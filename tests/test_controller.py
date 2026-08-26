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


def make_controller(adapter, reader, **overrides):
    clock = {"t": 1000.0}

    def now():
        return clock["t"]

    controller = FollowMeController(
        name="test",
        config=ControllerConfig(**overrides),
        adapter=adapter,
        sensor_reader=reader,
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
    # Sensor comes back fresh -> feedforward repositions.
    sensor.update(value=26.2, age=0.0)
    clock["t"] += 300
    tick(controller)
    assert controller.status == STATUS_ADJUSTING
    assert adapter.writes[-1] == 26.8  # 26 - (26.2 - 27)


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
