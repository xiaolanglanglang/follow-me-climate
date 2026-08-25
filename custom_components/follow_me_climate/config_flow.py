"""Config flow for Follow-Me Climate."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

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
    MAX_DEADBAND,
    MAX_INTERVAL,
    MAX_MANUAL_PAUSE,
    MAX_SENSOR_TIMEOUT,
    MAX_STEP,
    MAX_TARGET,
    MIN_DEADBAND,
    MIN_INTERVAL,
    MIN_MANUAL_PAUSE,
    MIN_SENSOR_TIMEOUT,
    MIN_STEP,
    MIN_TARGET,
    SP_ABS_MAX,
    SP_ABS_MIN,
)


def _build_schema(defaults: dict[str, Any]) -> vol.Schema:
    def default(key: str, fallback: Any) -> Any:
        return defaults.get(key, fallback)

    return vol.Schema(
        {
            vol.Required(
                CONF_NAME, default=default(CONF_NAME, DEFAULT_NAME)
            ): str,
            vol.Required(CONF_CLIMATE_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="climate")
            ),
            vol.Required(CONF_SENSOR_ENTITY): EntitySelector(
                EntitySelectorConfig(device_class="temperature")
            ),
            vol.Required(
                CONF_TARGET, default=default(CONF_TARGET, DEFAULT_TARGET)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_TARGET,
                    max=MAX_TARGET,
                    step=0.5,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_STEP, default=default(CONF_STEP, DEFAULT_STEP)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_STEP,
                    max=MAX_STEP,
                    step=0.5,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_INTERVAL, default=default(CONF_INTERVAL, DEFAULT_INTERVAL)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_INTERVAL,
                    max=MAX_INTERVAL,
                    step=1,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_DEADBAND, default=default(CONF_DEADBAND, DEFAULT_DEADBAND)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_DEADBAND,
                    max=MAX_DEADBAND,
                    step=0.1,
                    unit_of_measurement="°C",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MIN_SP, default=default(CONF_MIN_SP, DEFAULT_MIN_SP)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=SP_ABS_MIN,
                    max=SP_ABS_MAX,
                    step=0.5,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MAX_SP, default=default(CONF_MAX_SP, DEFAULT_MAX_SP)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=SP_ABS_MIN,
                    max=SP_ABS_MAX,
                    step=0.5,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SENSOR_TIMEOUT,
                default=default(CONF_SENSOR_TIMEOUT, DEFAULT_SENSOR_TIMEOUT),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_SENSOR_TIMEOUT,
                    max=MAX_SENSOR_TIMEOUT,
                    step=1,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MANUAL_PAUSE,
                default=default(CONF_MANUAL_PAUSE, DEFAULT_MANUAL_PAUSE),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_MANUAL_PAUSE,
                    max=MAX_MANUAL_PAUSE,
                    step=5,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_FEEDFORWARD, default=default(CONF_FEEDFORWARD, DEFAULT_FEEDFORWARD)
            ): BooleanSelector(),
            vol.Required(
                CONF_DRY_RUN, default=default(CONF_DRY_RUN, DEFAULT_DRY_RUN)
            ): BooleanSelector(),
        }
    )


class FollowMeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Follow-Me Climate config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            if user_input[CONF_MIN_SP] > user_input[CONF_MAX_SP]:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_build_schema(user_input),
                    errors={"base": "min_above_max"},
                )
            return self.async_create_entry(
                title=user_input[CONF_NAME], data=user_input
            )
        return self.async_show_form(step_id="user", data_schema=_build_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> FollowMeOptionsFlow:
        return FollowMeOptionsFlow()


class FollowMeOptionsFlow(config_entries.OptionsFlow):
    """Allow retuning all parameters without removing the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            if user_input[CONF_MIN_SP] > user_input[CONF_MAX_SP]:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_build_schema(user_input),
                    errors={"base": "min_above_max"},
                )
            return self.async_create_entry(title="", data=user_input)
        merged = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_build_schema(merged)
        )
