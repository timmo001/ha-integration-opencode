"""Config flow for the OpenCode integration."""

from collections.abc import Mapping
from typing import Any, override

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_MODEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PROMPT,
    CONF_TIMEOUT,
    CONF_URL,
)
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import probatio

from . import OpenCodeConfigEntry
from .api import OpenCodeAuthError, OpenCodeClient, OpenCodeError
from .const import (
    CONF_AGENT,
    CONF_DIRECTORY,
    CONF_VARIANT,
    DEFAULT_AGENT,
    DEFAULT_CONVERSATION_NAME,
    DEFAULT_TIMEOUT,
    DOMAIN,
    LOGGER,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
)

STEP_USER_DATA_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        probatio.Optional(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

STEP_REAUTH_DATA_SCHEMA = probatio.Schema(
    {
        probatio.Optional(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

DEFAULT_CONVERSATION_OPTIONS = {
    CONF_AGENT: DEFAULT_AGENT,
    CONF_PROMPT: llm.DEFAULT_INSTRUCTIONS_PROMPT,
    CONF_TIMEOUT: DEFAULT_TIMEOUT,
}


class OpenCodeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenCode."""

    VERSION = 1

    async def _async_validate(self, url: str, password: str | None) -> dict[str, str]:
        """Check the server answers and accepts the password."""
        client = OpenCodeClient(async_get_clientsession(self.hass), url, password)
        try:
            await client.async_get_agents()
        except OpenCodeAuthError:
            return {"base": "invalid_auth"}
        except OpenCodeError:
            return {"base": "cannot_connect"}
        except Exception:
            LOGGER.exception("Unexpected exception")
            return {"base": "unknown"}
        return {}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            password = user_input.get(CONF_PASSWORD)
            self._async_abort_entries_match({CONF_URL: url})
            if not (errors := await self._async_validate(url, password)):
                data = {CONF_URL: url}
                if password:
                    data[CONF_PASSWORD] = password
                return self.async_create_entry(
                    title=url,
                    data=data,
                    subentries=[
                        {
                            "subentry_type": "conversation",
                            "data": DEFAULT_CONVERSATION_OPTIONS,
                            "title": DEFAULT_CONVERSATION_NAME,
                            "unique_id": None,
                        }
                    ],
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a password the server no longer accepts."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            password = user_input.get(CONF_PASSWORD)
            if not (
                errors := await self._async_validate(entry.data[CONF_URL], password)
            ):
                data = {CONF_URL: entry.data[CONF_URL]}
                if password:
                    data[CONF_PASSWORD] = password
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={CONF_URL: entry.data[CONF_URL]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the server's address or password."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            password = user_input.get(CONF_PASSWORD)
            if url != entry.data[CONF_URL]:
                self._async_abort_entries_match({CONF_URL: url})
            if not (errors := await self._async_validate(url, password)):
                data = {CONF_URL: url}
                if password:
                    data[CONF_PASSWORD] = password
                return self.async_update_reload_and_abort(entry, title=url, data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input or {CONF_URL: entry.data[CONF_URL]}
            ),
            errors=errors,
        )

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: OpenCodeConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries this integration supports."""
        return {"conversation": OpenCodeSubentryFlowHandler}


class OpenCodeSubentryFlowHandler(ConfigSubentryFlow):
    """Add or change an OpenCode conversation agent."""

    async def async_step_set_options(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose the agent, model and how it answers."""
        entry: OpenCodeConfigEntry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        is_new = self.source == "user"
        if user_input is not None:
            if is_new:
                name = user_input.pop(CONF_NAME)
                return self.async_create_entry(title=name, data=user_input)
            return self.async_update_and_abort(
                entry, self._get_reconfigure_subentry(), data=user_input
            )

        client = entry.runtime_data.client
        try:
            agents = await client.async_get_agents()
            models = await client.async_get_models()
        except OpenCodeError:
            return self.async_abort(reason="cannot_connect")

        options = (
            DEFAULT_CONVERSATION_OPTIONS
            if is_new
            else self._get_reconfigure_subentry().data
        )
        schema: dict[Any, Any] = {}
        if is_new:
            # pylint: disable-next=home-assistant-config-flow-name-field
            schema[probatio.Required(CONF_NAME, default=DEFAULT_CONVERSATION_NAME)] = (
                str
            )
        schema.update(
            {
                probatio.Required(
                    CONF_AGENT,
                    description={"suggested_value": options.get(CONF_AGENT)},
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=agent["id"], label=agent["name"])
                            for agent in agents
                            if agent.get("mode") != "subagent"
                            and not agent.get("hidden")
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
                probatio.Optional(
                    CONF_MODEL,
                    description={"suggested_value": options.get(CONF_MODEL)},
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=sorted(
                            (
                                SelectOptionDict(
                                    value=f"{model['providerID']}/{model['id']}",
                                    label=f"{model['name']} ({model['providerID']})",
                                )
                                for model in models
                                if model.get("enabled", True)
                            ),
                            key=lambda option: option["label"],
                        ),
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
                probatio.Optional(
                    CONF_VARIANT,
                    description={"suggested_value": options.get(CONF_VARIANT)},
                ): TextSelector(),
                probatio.Optional(
                    CONF_DIRECTORY,
                    description={"suggested_value": options.get(CONF_DIRECTORY)},
                ): TextSelector(),
                probatio.Optional(
                    CONF_PROMPT,
                    description={"suggested_value": options.get(CONF_PROMPT)},
                ): TemplateSelector(),
                probatio.Required(
                    CONF_TIMEOUT,
                    default=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_TIMEOUT,
                        max=MAX_TIMEOUT,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="set_options", data_schema=probatio.Schema(schema)
        )

    async_step_user = async_step_set_options
    async_step_reconfigure = async_step_set_options
