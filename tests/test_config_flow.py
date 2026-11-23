"""Tests for the OpenCode config flow."""

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_MODEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PROMPT,
    CONF_TIMEOUT,
    CONF_URL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opencode.api import OpenCodeAuthError, OpenCodeConnectionError
from custom_components.opencode.const import (
    CONF_AGENT,
    CONF_DIRECTORY,
    CONF_VARIANT,
    DOMAIN,
)

from .conftest import URL, FakeOpenCode


async def test_user_flow(hass: HomeAssistant, mock_opencode: FakeOpenCode) -> None:
    """Test adding a server creates it with a default agent."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f" {URL}/ ", CONF_PASSWORD: "secret"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == URL
    assert result["data"] == {CONF_URL: URL, CONF_PASSWORD: "secret"}
    subentries = list(result["result"].subentries.values())
    assert len(subentries) == 1
    assert subentries[0].subentry_type == "conversation"
    assert subentries[0].data[CONF_AGENT] == "build"


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        pytest.param(OpenCodeConnectionError, "cannot_connect", id="cannot_connect"),
        pytest.param(OpenCodeAuthError, "invalid_auth", id="invalid_auth"),
        pytest.param(RuntimeError, "unknown", id="unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    mock_opencode: FakeOpenCode,
    side_effect: type[Exception],
    error: str,
) -> None:
    """Test the form shows the error and recovers."""
    mock_opencode.async_get_agents.side_effect = side_effect
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_opencode.async_get_agents.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: URL}


async def test_user_flow_already_configured(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the same server can't be added twice."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test a new password replaces the old one."""
    result = await init_integration.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert init_integration.data == {CONF_URL: URL, CONF_PASSWORD: "new"}


async def test_reconfigure_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the server's address can change."""
    result = await init_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://other.local:4096"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert init_integration.data == {CONF_URL: "http://other.local:4096"}
    assert init_integration.title == "http://other.local:4096"


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


async def test_reconfigure_conversation_subentry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test changing an agent's options."""
    subentry = next(iter(init_integration.subentries.values()))
    result = await init_integration.start_subentry_reconfigure_flow(
        hass, subentry.subentry_id
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_AGENT: "plan", CONF_TIMEOUT: 30}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert init_integration.subentries[subentry.subentry_id].data == {
        CONF_AGENT: "plan",
        CONF_TIMEOUT: 30,
    }
