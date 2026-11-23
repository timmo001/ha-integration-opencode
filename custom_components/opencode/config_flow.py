"""Config flow for the OpenCode integration."""

from collections.abc import Mapping
from typing import Any, override

from homeassistant.config_entries import (
    SOURCE_HASSIO,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_LLM_HASS_API,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONF_PROMPT,
    CONF_TIMEOUT,
    CONF_TOKEN,
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
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.util import dt as dt_util
import probatio
from yarl import URL

from . import OpenCodeConfigEntry
from .api import (
    OpenCodeAuthError,
    OpenCodeClient,
    OpenCodeError,
    parse_pairing_link,
)
from .const import (
    CONF_AGENT,
    CONF_DIRECTORY,
    CONF_LINK,
    CONF_RENEWED_AT,
    CONF_VARIANT,
    DEFAULT_AGENT,
    DEFAULT_AI_TASK_TIMEOUT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    LOGGER,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
)

STEP_LINK_DATA_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_LINK): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
    }
)

DEFAULT_CONVERSATION_OPTIONS = {
    CONF_AGENT: DEFAULT_AGENT,
    CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
    CONF_PROMPT: llm.DEFAULT_INSTRUCTIONS_PROMPT,
    CONF_TIMEOUT: DEFAULT_TIMEOUT,
}
DEFAULT_AI_TASK_OPTIONS = {
    CONF_AGENT: DEFAULT_AGENT,
    CONF_TIMEOUT: DEFAULT_AI_TASK_TIMEOUT,
}


def _input_price(model: dict[str, Any]) -> float:
    """Return a model's price per million input tokens, 0 when free or unknown."""
    cost = model.get("cost")
    if isinstance(cost, list):
        cost = cost[0] if cost else None
    if not isinstance(cost, dict):
        return 0
    return cost.get("input") or 0


def suggest_small_model(models: list[dict[str, Any]]) -> str | None:
    """Pick the cheapest paid model that can use tools, the newest on a tie.

    Free models are left out, as they're usually trials that log prompts.
    """
    candidates = [
        model
        for model in models
        if model.get("enabled", True)
        and model.get("capabilities", {}).get("tools")
        and _input_price(model) > 0
    ]
    if not candidates:
        return None
    model = min(
        candidates,
        key=lambda model: (
            _input_price(model),
            -model.get("time", {}).get("released", 0),
        ),
    )
    return f"{model['providerID']}/{model['id']}"


EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def _variants(models: list[dict[str, Any]]) -> list[str]:
    """Return the variants the models offer, lowest effort first."""
    variants = {
        variant["id"]
        for model in models
        if model.get("enabled", True)
        for variant in model.get("variants") or []
    }
    return sorted(
        variants,
        key=lambda variant: (
            EFFORT_ORDER.index(variant)
            if variant in EFFORT_ORDER
            else len(EFFORT_ORDER),
            variant,
        ),
    )


def model_selector(models: list[dict[str, Any]]) -> SelectSelector:
    """Return a dropdown of the server's models."""
    return SelectSelector(
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
        )
    )


def _find_model(
    models: list[dict[str, Any]], model_ref: str | None
) -> dict[str, Any] | None:
    """Return the model a provider/model reference points at."""
    return next(
        (
            model
            for model in models
            if f"{model['providerID']}/{model['id']}" == model_ref
        ),
        None,
    )


APP_SERVER_NAME = "app"


def server_name(entry: OpenCodeConfigEntry) -> str:
    """Return what agents call their server: the app, or the entry's name."""
    return APP_SERVER_NAME if entry.source == SOURCE_HASSIO else entry.title


def default_title(
    server: str,
    data: Mapping[str, Any],
    models: list[dict[str, Any]],
    *,
    ai_task: bool,
) -> str:
    """Name an agent or AI task after its model and server, to tell them apart."""
    name = "OpenCode"
    if model_ref := data.get(CONF_MODEL):
        model = _find_model(models, model_ref)
        name = model["name"] if model else model_ref.partition("/")[2]
    if variant := data.get(CONF_VARIANT):
        name = f"{name} {variant}"
    if ai_task:
        name = f"{name} AI task"
    return f"{name} ({server})"


class OpenCodeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenCode."""

    VERSION = 1

    _discovery: HassioServiceInfo
    _data: dict[str, Any]
    _title: str
    # What the server is called in its agents' names
    _server: str
    _models: list[dict[str, Any]]

    def _async_update_and_finish(
        self, entry: OpenCodeConfigEntry, reason: str, **changes: Any
    ) -> ConfigFlowResult:
        """Update an entry, leaving the reload to its update listener if loaded."""
        if entry.update_listeners:
            self.hass.config_entries.async_update_entry(entry, **changes)
            return self.async_abort(reason=reason)
        return self.async_update_reload_and_abort(
            entry, reason=reason, reload_even_if_entry_is_unchanged=False, **changes
        )

    async def _async_pair(
        self, link: str, current_url: str | None = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Redeem a pairing link, returning the entry data or the form errors."""
        try:
            url, code = parse_pairing_link(link)
        except ValueError:
            return {}, {CONF_LINK: "invalid_link"}
        if url != current_url:
            self._async_abort_entries_match({CONF_URL: url})
        client = OpenCodeClient(async_get_clientsession(self.hass), url, None)
        try:
            token = await client.async_redeem(code)
            client.set_token(token)
            info = await client.async_get_info()
            await client.async_get_agents()
            self._models = await client.async_get_models()
        except OpenCodeAuthError:
            return {}, {CONF_LINK: "expired_link"}
        except OpenCodeError:
            return {}, {"base": "cannot_connect"}
        except Exception:
            LOGGER.exception("Unexpected exception")
            return {}, {"base": "unknown"}
        self._title = self._server = info.get("hostname") or URL(url).host or url
        return {
            CONF_URL: url,
            CONF_TOKEN: token,
            CONF_RENEWED_AT: dt_util.utcnow().timestamp(),
        }, {}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair with a server from a link it printed."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._async_pair(user_input[CONF_LINK])
            if not errors:
                self._data = data
                return await self.async_step_model()

        return self.async_show_form(
            step_id="user", data_schema=STEP_LINK_DATA_SCHEMA, errors=errors
        )

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the model the first agent and AI task use."""
        if user_input is not None or not self._models:
            conversation = {**DEFAULT_CONVERSATION_OPTIONS, **(user_input or {})}
            ai_task = {**DEFAULT_AI_TASK_OPTIONS, **(user_input or {})}
            return self.async_create_entry(
                title=self._title,
                data=self._data,
                subentries=[
                    ConfigSubentryData(
                        subentry_type="conversation",
                        data=conversation,
                        title=default_title(
                            self._server, conversation, self._models, ai_task=False
                        ),
                        unique_id=None,
                    ),
                    ConfigSubentryData(
                        subentry_type="ai_task_data",
                        data=ai_task,
                        title=default_title(
                            self._server, ai_task, self._models, ai_task=True
                        ),
                        unique_id=None,
                    ),
                ],
            )

        return self.async_show_form(
            step_id="model",
            data_schema=probatio.Schema(
                {
                    probatio.Optional(
                        CONF_MODEL,
                        description={
                            "suggested_value": suggest_small_model(self._models)
                        },
                    ): model_selector(self._models),
                }
            ),
        )

    @override
    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle the OpenCode app, which pairs Home Assistant for us."""
        await self.async_set_unique_id(discovery_info.uuid)
        url = f"http://{discovery_info.config[CONF_HOST]}:{discovery_info.config[CONF_PORT]}"
        if entry := self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, discovery_info.uuid
        ):
            data = {**entry.data, CONF_URL: url}
            if entry.state is not ConfigEntryState.LOADED:
                # The app pairs on every start, so its token mends an entry
                # the server stopped accepting
                data |= {
                    CONF_TOKEN: discovery_info.config[CONF_TOKEN],
                    CONF_RENEWED_AT: dt_util.utcnow().timestamp(),
                }
            return self._async_update_and_finish(entry, "already_configured", data=data)
        self._async_abort_entries_match({CONF_URL: url})

        self._discovery = discovery_info
        self.context.update(
            {
                "title_placeholders": {"name": discovery_info.name},
                "configuration_url": (
                    f"homeassistant://config/app/{discovery_info.slug}/info"
                ),
            }
        )
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add the OpenCode app."""
        errors: dict[str, str] = {}
        if user_input is not None:
            config = self._discovery.config
            url = f"http://{config[CONF_HOST]}:{config[CONF_PORT]}"
            client = OpenCodeClient(
                async_get_clientsession(self.hass), url, config[CONF_TOKEN]
            )
            try:
                await client.async_get_agents()
                self._models = await client.async_get_models()
            except OpenCodeAuthError:
                return self.async_abort(reason="app_pairing_expired")
            except OpenCodeError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self._data = {
                    CONF_URL: url,
                    CONF_TOKEN: config[CONF_TOKEN],
                    CONF_RENEWED_AT: dt_util.utcnow().timestamp(),
                }
                self._title = self._discovery.name
                self._server = APP_SERVER_NAME
                return await self.async_step_model()

        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"app": self._discovery.name},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a token the server no longer accepts."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again with a new link."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data, errors = await self._async_pair(
                user_input[CONF_LINK], entry.data[CONF_URL]
            )
            if not errors:
                return self._async_update_and_finish(
                    entry, "reauth_successful", data=data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_LINK_DATA_SCHEMA,
            description_placeholders={CONF_URL: entry.data[CONF_URL]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again, such as when the server's address changes."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            data, errors = await self._async_pair(
                user_input[CONF_LINK], entry.data[CONF_URL]
            )
            if not errors:
                return self._async_update_and_finish(
                    entry, "reconfigure_successful", data=data
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=STEP_LINK_DATA_SCHEMA,
            description_placeholders={CONF_URL: entry.data[CONF_URL]},
            errors=errors,
        )

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: OpenCodeConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries this integration supports."""
        return {
            "conversation": OpenCodeSubentryFlowHandler,
            "ai_task_data": OpenCodeSubentryFlowHandler,
        }


class OpenCodeSubentryFlowHandler(ConfigSubentryFlow):
    """Add or change an OpenCode conversation agent or AI task."""

    async def async_step_set_options(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose the agent, model and how it answers."""
        entry: OpenCodeConfigEntry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        is_new = self.source == "user"
        is_ai_task = self._subentry_type == "ai_task_data"
        client = entry.runtime_data.client
        try:
            agents = await client.async_get_agents()
            models = await client.async_get_models()
        except OpenCodeError:
            return self.async_abort(reason="cannot_connect")

        errors: dict[str, str] = {}
        if user_input is not None:
            if (variant := user_input.get(CONF_VARIANT)) and (
                model := _find_model(models, user_input.get(CONF_MODEL))
            ):
                if variant not in _variants([model]):
                    errors[CONF_VARIANT] = "unsupported_variant"
            if not errors:
                server = server_name(entry)
                if is_new:
                    name = user_input.pop(CONF_NAME, None) or default_title(
                        server, user_input, models, ai_task=is_ai_task
                    )
                    return self.async_create_entry(title=name, data=user_input)
                subentry = self._get_reconfigure_subentry()
                changes: dict[str, Any] = {"data": user_input}
                # A name nobody changed follows the model
                if subentry.title == default_title(
                    server, subentry.data, models, ai_task=is_ai_task
                ):
                    changes["title"] = default_title(
                        server, user_input, models, ai_task=is_ai_task
                    )
                return self.async_update_and_abort(entry, subentry, **changes)

        if user_input is not None:
            options: Mapping[str, Any] = user_input
            model_ref = user_input.get(CONF_MODEL)
        elif is_new:
            options = (
                DEFAULT_AI_TASK_OPTIONS if is_ai_task else DEFAULT_CONVERSATION_OPTIONS
            )
            model_ref = suggest_small_model(models)
        else:
            options = self._get_reconfigure_subentry().data
            model_ref = options.get(CONF_MODEL)
        schema: dict[Any, Any] = {}
        if is_new:
            # pylint: disable-next=home-assistant-config-flow-name-field
            schema[
                probatio.Optional(
                    CONF_NAME, description={"suggested_value": options.get(CONF_NAME)}
                )
            ] = str
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
                    CONF_MODEL, description={"suggested_value": model_ref}
                ): model_selector(models),
                probatio.Optional(
                    CONF_VARIANT,
                    description={"suggested_value": options.get(CONF_VARIANT)},
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_variants(models), mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                probatio.Optional(
                    CONF_DIRECTORY,
                    description={"suggested_value": options.get(CONF_DIRECTORY)},
                ): TextSelector(),
            }
        )
        if not is_ai_task:
            schema[
                probatio.Optional(
                    CONF_PROMPT,
                    description={"suggested_value": options.get(CONF_PROMPT)},
                )
            ] = TemplateSelector()
            apis = llm.async_get_apis(self.hass)
            known_apis = {api.id for api in apis}
            schema[
                probatio.Optional(
                    CONF_LLM_HASS_API,
                    description={
                        "suggested_value": [
                            api
                            for api in options.get(CONF_LLM_HASS_API) or []
                            if api in known_apis
                        ]
                    },
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=api.id, label=api.name) for api in apis
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        schema.update(
            {
                probatio.Required(
                    CONF_TIMEOUT,
                    default=options.get(
                        CONF_TIMEOUT,
                        DEFAULT_AI_TASK_TIMEOUT if is_ai_task else DEFAULT_TIMEOUT,
                    ),
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
            step_id="set_options", data_schema=probatio.Schema(schema), errors=errors
        )

    async_step_user = async_step_set_options
    async_step_reconfigure = async_step_set_options
