"""Constants for the Follow-Me Climate integration."""

DOMAIN = "follow_me_climate"

CONF_NAME = "name"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_SENSOR_ENTITY = "reference_sensor"
CONF_POWER_ENTITY = "power_sensor"
CONF_TARGET = "target_temperature"
CONF_STEP = "step"
CONF_INTERVAL = "adjust_interval"
CONF_DEADBAND = "deadband"
CONF_MIN_SP = "min_setpoint"
CONF_MAX_SP = "max_setpoint"
CONF_SENSOR_TIMEOUT = "sensor_timeout"
CONF_MANUAL_PAUSE = "manual_pause"
CONF_FEEDFORWARD = "feedforward"
CONF_DRY_RUN = "dry_run"
CONF_FOLLOW_POWER = "follow_climate_power"

DEFAULT_NAME = "Follow-Me Climate"
DEFAULT_TARGET = 26.0
DEFAULT_STEP = 0.5
DEFAULT_INTERVAL = 4.0
DEFAULT_DEADBAND = 0.5
DEFAULT_MIN_SP = 16.0
DEFAULT_MAX_SP = 30.0
DEFAULT_SENSOR_TIMEOUT = 10.0
DEFAULT_MANUAL_PAUSE = 30.0
DEFAULT_FEEDFORWARD = True
DEFAULT_DRY_RUN = False
DEFAULT_FOLLOW_POWER = True

MIN_TARGET = 16.0
MAX_TARGET = 30.0
MIN_STEP = 0.5
MAX_STEP = 2.0
MIN_INTERVAL = 1.0
MAX_INTERVAL = 15.0
MIN_DEADBAND = 0.1
MAX_DEADBAND = 1.5
SP_ABS_MIN = 16.0
SP_ABS_MAX = 30.0
MIN_SENSOR_TIMEOUT = 1.0
MAX_SENSOR_TIMEOUT = 60.0
MIN_MANUAL_PAUSE = 0.0
MAX_MANUAL_PAUSE = 120.0

# HVAC mode values, matching homeassistant.components.climate.HVACMode
HVAC_COOL = "cool"
HVAC_HEAT = "heat"
HVAC_OFF = "off"

STATUS_INACTIVE = "inactive"
STATUS_IDLE = "idle"
STATUS_ADJUSTING = "adjusting"
STATUS_MANUAL_PAUSE = "manual_pause"
STATUS_SENSOR_LOST = "sensor_lost"

ALL_STATUSES = [
    STATUS_INACTIVE,
    STATUS_IDLE,
    STATUS_ADJUSTING,
    STATUS_MANUAL_PAUSE,
    STATUS_SENSOR_LOST,
]

# Power-momentum gate states (the status sensor's power_gate attribute).
POWER_GATE_OFF = "off"  # no power sensor wired
POWER_GATE_UNAVAILABLE = "unavailable"  # sensor missing/stale, gates inert
POWER_GATE_WAITING = "waiting"  # evidence lag window after a setpoint write
POWER_GATE_HOLDING = "holding"  # momentum confirmed, stepping deferred
POWER_GATE_OPEN = "open"  # gates passed, stepping allowed

# Power-gate tuning; not user-facing options until proven on real hardware.
# Smart-plug evidence trails a setpoint write (inverter ramp-up plus the
# plug's aggregation lag), so power is only judged after this window.
POWER_RESPONSE_LAG = 180.0  # seconds after a write before power is judged
POWER_RISE_RATIO = 0.30  # fraction of the baseline counted as a real rise
POWER_RISE_MIN_W = 30.0  # absolute floor of that rise (standby baselines)
POWER_HOLD_TIMEOUT = 15 * 60.0  # seconds a momentum hold may defer stepping
POWER_STALE_TIMEOUT = 5 * 60.0  # seconds before power counts as unavailable

# Options that can change at runtime without reloading the config entry.
RUNTIME_KEYS = {
    CONF_TARGET,
    CONF_STEP,
    CONF_INTERVAL,
    CONF_DEADBAND,
    CONF_SENSOR_TIMEOUT,
    CONF_MANUAL_PAUSE,
    CONF_FEEDFORWARD,
    CONF_DRY_RUN,
    CONF_FOLLOW_POWER,
}

# Options that require a reload (they change entity wiring or bounds).
STRUCTURAL_KEYS = {
    CONF_NAME,
    CONF_CLIMATE_ENTITY,
    CONF_SENSOR_ENTITY,
    CONF_POWER_ENTITY,
    CONF_MIN_SP,
    CONF_MAX_SP,
}
