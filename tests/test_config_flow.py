"""Tests for the OpenCode config flow."""

import re

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_MODEL,
    CONF_NAME,
    CONF_PROMPT,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_URL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opencode.api import (
    OpenCodeAuthError,
    OpenCodeConnectionError,
    parse_pairing_link,
)
from custom_components.opencode.const import (
    CONF_AGENT,
    CONF_DIRECTORY,
    CONF_LINK,
    CONF_RENEWED_AT,
    CONF_VARIANT,
    DOMAIN,
)

from .conftest import LINK, URL, FakeOpenCode


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
    """Test pairing with a server creates it with a default agent."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LINK: LINK}
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
    subentries = list(result["result"].subentries.values())
    assert len(subentries) == 1
    assert subentries[0].subentry_type == "conversation"
    assert subentries[0].data[CONF_AGENT] == "build"


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
