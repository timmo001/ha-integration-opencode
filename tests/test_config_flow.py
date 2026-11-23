"""Tests for the OpenCode config flow."""

import re
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONF_PROMPT,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_URL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import llm
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opencode.api import (
    OpenCodeAuthError,
    OpenCodeConnectionError,
    parse_pairing_link,
)
from custom_components.opencode.config_flow import suggest_small_model
from custom_components.opencode.const import (
    CONF_AGENT,
    CONF_DIRECTORY,
    CONF_LINK,
    CONF_RENEWED_AT,
    CONF_VARIANT,
    DOMAIN,
)

from .conftest import LINK, MODELS, SMALL_MODEL, URL, FakeOpenCode

APP_URL = "http://a0d7b954-opencode:49374"
HASSIO_DISCOVERY = HassioServiceInfo(
    config={CONF_HOST: "a0d7b954-opencode", CONF_PORT: 49374, CONF_TOKEN: "app-token"},
    name="OpenCode",
    slug="a0d7b954_opencode",
    uuid="1234",
)


def suggested_value(result: dict[str, Any], key: str) -> Any:
    """Return the value a form pre-fills for a field."""
    for marker in result["data_schema"].schema:
        if marker == key:
            return (marker.description or {}).get("suggested_value")
    raise KeyError(key)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (LINK, (URL, "abc123")),
        (f" {LINK}?x=1#y ", (URL, "abc123")),
        (
            "https://abc.opentunnel.xyz/prefix/auth/connect/abc123",
            ("https://abc.opentunnel.xyz/prefix", "abc123"),
        ),
    ],
)
def test_parse_pairing_link(link: str, expected: tuple[str, str]) -> None:
    """Test the server's address and code come out of a pairing link."""
    assert parse_pairing_link(link) == expected


@pytest.mark.parametrize(
    "link",
    [
        URL,
        f"{URL}/auth/connect/",
        f"{URL}/auth/connect/abc/def",
        "ftp://opencode.local/auth/connect/abc",
        "/auth/connect/abc",
    ],
)
def test_parse_pairing_link_invalid(link: str) -> None:
    """Test anything but a pairing link is refused."""
    with pytest.raises(ValueError, match=re.escape(link)):
        parse_pairing_link(link)


async def test_user_flow(
    hass: HomeAssistant, mock_opencode: FakeOpenCode, freezer: FrozenDateTimeFactory
) -> None:
    """Test pairing with a server, then picking the first agent's model."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: LINK}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "model"
    assert suggested_value(result, CONF_MODEL) == SMALL_MODEL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL: "github-copilot/claude-haiku-5.5"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == URL
    assert result["data"] == {
        CONF_URL: URL,
        CONF_TOKEN: "paired-token",
        CONF_RENEWED_AT: dt_util.utcnow().timestamp(),
    }
    mock_opencode.async_redeem.assert_awaited_once_with("abc123")
    mock_opencode.set_token.assert_called_once_with("paired-token")
    subentries = {
        subentry.subentry_type: subentry.data
        for subentry in result["result"].subentries.values()
    }
    assert subentries == {
        "conversation": {
            CONF_AGENT: "build",
            CONF_MODEL: "github-copilot/claude-haiku-5.5",
            CONF_PROMPT: llm.DEFAULT_INSTRUCTIONS_PROMPT,
            CONF_TIMEOUT: 60,
        },
        "ai_task_data": {
            CONF_AGENT: "build",
            CONF_MODEL: "github-copilot/claude-haiku-5.5",
            CONF_TIMEOUT: 600,
        },
    }


async def test_user_flow_agent_default_model(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test clearing the model leaves it to the agent."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: LINK}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(iter(result["result"].subentries.values()))
    assert CONF_MODEL not in subentry.data


async def test_user_flow_no_models(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a server with no models skips the model step."""
    mock_opencode.async_get_models.return_value = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: LINK}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        pytest.param(MODELS, SMALL_MODEL, id="cheapest_newest"),
        pytest.param(
            MODELS[:1] + MODELS[2:], "github-copilot/claude-haiku-5.5", id="cheapest"
        ),
        pytest.param(
            [{**MODELS[1], "enabled": False}, MODELS[2]],
            "github-copilot/gemini-4-neon",
            id="disabled",
        ),
        pytest.param(
            [{**MODELS[1], "capabilities": {"tools": False}}, MODELS[2]],
            "github-copilot/gemini-4-neon",
            id="no_tools",
        ),
        pytest.param(MODELS[3:], None, id="only_free"),
        pytest.param([{"id": "x", "providerID": "y"}], None, id="no_details"),
    ],
)
def test_suggest_small_model(models: list[dict[str, Any]], expected: str) -> None:
    """Test the suggested model is the cheapest paid one that can use tools."""
    assert suggest_small_model(models) == expected


@pytest.mark.parametrize(
    ("link", "side_effect", "errors"),
    [
        pytest.param(URL, None, {CONF_LINK: "invalid_link"}, id="invalid_link"),
        pytest.param(
            LINK, OpenCodeAuthError, {CONF_LINK: "expired_link"}, id="expired"
        ),
        pytest.param(
            LINK,
            OpenCodeConnectionError,
            {"base": "cannot_connect"},
            id="cannot_connect",
        ),
        pytest.param(LINK, RuntimeError, {"base": "unknown"}, id="unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    mock_opencode: FakeOpenCode,
    link: str,
    side_effect: type[Exception] | None,
    errors: dict[str, str],
) -> None:
    """Test the form shows the error and recovers with a new link."""
    mock_opencode.async_redeem.side_effect = side_effect
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: link}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors

    mock_opencode.async_redeem.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: LINK}
    )
    assert result["step_id"] == "model"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_already_configured(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_opencode: FakeOpenCode,
) -> None:
    """Test the same server can't be added twice, without using up the link."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: LINK}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_opencode.async_redeem.assert_not_awaited()


async def test_reauth_flow(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_opencode: FakeOpenCode,
) -> None:
    """Test a new pairing link replaces the token."""
    result = await init_integration.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: LINK}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert init_integration.data[CONF_URL] == URL
    assert init_integration.data[CONF_TOKEN] == "paired-token"


async def test_reconfigure_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test pairing again can move the server to a new address."""
    result = await init_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: "http://other.local:49374/auth/connect/xyz"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert init_integration.data[CONF_URL] == "http://other.local:49374"
    assert init_integration.data[CONF_TOKEN] == "paired-token"
    assert init_integration.title == "http://other.local:49374"


async def test_hassio_flow(
    hass: HomeAssistant, mock_opencode: FakeOpenCode, freezer: FrozenDateTimeFactory
) -> None:
    """Test the OpenCode app is added with the token it paired."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO_DISCOVERY
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["description_placeholders"] == {"app": "OpenCode"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "model"
    assert suggested_value(result, CONF_MODEL) == SMALL_MODEL
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL: SMALL_MODEL}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "OpenCode"
    assert result["result"].unique_id == "1234"
    assert result["data"] == {
        CONF_URL: APP_URL,
        CONF_TOKEN: "app-token",
        CONF_RENEWED_AT: dt_util.utcnow().timestamp(),
    }
    mock_opencode.async_redeem.assert_not_awaited()
    subentry = next(iter(result["result"].subentries.values()))
    assert subentry.data[CONF_MODEL] == SMALL_MODEL


async def test_hassio_flow_cannot_connect(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test the confirm step shows an app it can't reach yet, and recovers."""
    mock_opencode.async_get_agents.side_effect = OpenCodeConnectionError
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO_DISCOVERY
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    mock_opencode.async_get_agents.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "model"


async def test_hassio_flow_pairing_expired(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a token the app no longer accepts asks for the app to restart."""
    mock_opencode.async_get_agents.side_effect = OpenCodeAuthError
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO_DISCOVERY
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "app_pairing_expired"


async def test_hassio_flow_already_configured(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a loaded app keeps its own token, but follows a new address."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234",
        data={
            CONF_URL: "http://old-opencode:49374",
            CONF_TOKEN: "token",
            CONF_RENEWED_AT: dt_util.utcnow().timestamp(),
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO_DISCOVERY
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_URL] == APP_URL
    assert entry.data[CONF_TOKEN] == "token"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data[CONF_URL] == APP_URL


async def test_hassio_flow_mends_entry(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test an app whose token was refused takes the newly paired one."""
    mock_opencode.async_get_info.side_effect = OpenCodeAuthError
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234",
        data={CONF_URL: APP_URL, CONF_TOKEN: "token"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    mock_opencode.async_get_info.side_effect = None
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO_DISCOVERY
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_TOKEN] == "app-token"
    assert entry.state is ConfigEntryState.LOADED


async def test_hassio_flow_added_by_link(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test an app already paired by hand from the same address isn't offered."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={CONF_HOST: "opencode.local", CONF_PORT: 49374, CONF_TOKEN: "t"},
            name="OpenCode",
            slug="opencode",
            uuid="1234",
        ),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_add_conversation_subentry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test adding another agent lists the server's primary agents."""
    result = await hass.config_entries.subentries.async_init(
        (init_integration.entry_id, "conversation"),
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    agent_options = [
        option["value"]
        for option in result["data_schema"].schema[CONF_AGENT].config["options"]
    ]
    assert agent_options == ["build", "plan"]
    assert suggested_value(result, CONF_MODEL) == SMALL_MODEL
    assert result["data_schema"].schema[CONF_VARIANT].config["options"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "custom",
    ]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Research",
            CONF_AGENT: "plan",
            CONF_MODEL: "github-copilot/claude-haiku-5.5",
            CONF_VARIANT: "high",
            CONF_DIRECTORY: "/srv/assist",
            CONF_PROMPT: "Answer briefly.",
            CONF_TIMEOUT: 120,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Research"
    assert result["data"] == {
        CONF_AGENT: "plan",
        CONF_MODEL: "github-copilot/claude-haiku-5.5",
        CONF_VARIANT: "high",
        CONF_DIRECTORY: "/srv/assist",
        CONF_PROMPT: "Answer briefly.",
        CONF_TIMEOUT: 120,
    }


async def test_add_conversation_subentry_unsupported_variant(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test a variant the picked model lacks is refused, keeping what was entered."""
    result = await hass.config_entries.subentries.async_init(
        (init_integration.entry_id, "conversation"),
        context={"source": SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Research",
            CONF_AGENT: "plan",
            CONF_MODEL: "github-copilot/claude-haiku-5.5",
            CONF_VARIANT: "xhigh",
            CONF_TIMEOUT: 120,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VARIANT: "unsupported_variant"}
    assert suggested_value(result, CONF_MODEL) == "github-copilot/claude-haiku-5.5"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Research",
            CONF_AGENT: "plan",
            CONF_MODEL: "github-copilot/gemini-4-neon",
            CONF_VARIANT: "xhigh",
            CONF_TIMEOUT: 120,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VARIANT] == "xhigh"


async def test_add_ai_task_subentry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test adding an AI task, which has no instructions and a longer timeout."""
    result = await hass.config_entries.subentries.async_init(
        (init_integration.entry_id, "ai_task_data"),
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert CONF_PROMPT not in result["data_schema"].schema
    assert suggested_value(result, CONF_MODEL) == SMALL_MODEL
    assert result["data_schema"]({CONF_AGENT: "build"})[CONF_TIMEOUT] == 600
    assert result["data_schema"]({CONF_AGENT: "build"})[CONF_NAME] == (
        "OpenCode AI Task"
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Research", CONF_AGENT: "plan", CONF_TIMEOUT: 1800},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Research"
    assert result["data"] == {CONF_AGENT: "plan", CONF_TIMEOUT: 1800}


async def test_reconfigure_conversation_subentry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test changing an agent's options."""
    subentry = next(iter(init_integration.subentries.values()))
    hass.config_entries.async_update_subentry(
        init_integration,
        subentry,
        data={**subentry.data, CONF_MODEL: "github-copilot/gemini-4-neon"},
    )
    await hass.async_block_till_done()
    result = await init_integration.start_subentry_reconfigure_flow(
        hass, subentry.subentry_id
    )
    assert suggested_value(result, CONF_MODEL) == "github-copilot/gemini-4-neon"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_AGENT: "plan", CONF_TIMEOUT: 30}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert init_integration.subentries[subentry.subentry_id].data == {
        CONF_AGENT: "plan",
        CONF_TIMEOUT: 30,
    }
