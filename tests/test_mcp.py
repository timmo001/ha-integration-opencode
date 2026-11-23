"""Tests for serving Home Assistant's tools to OpenCode sessions."""

import dataclasses
from unittest.mock import MagicMock

from homeassistant.components import conversation
from homeassistant.const import (
    CONF_LLM_HASS_API,
    CONF_PROMPT,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_URL,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opencode.const import (
    CONF_AGENT,
    CONF_RENEWED_AT,
    DOMAIN,
    TOOLS_PROMPT,
)
from custom_components.opencode.mcp import (
    NOT_OUR_SESSION,
    SESSION_ID_META,
    HomeAssistantTools,
)

from .conftest import URL, FakeOpenCode, text_turn

AGENT_ID = "conversation.opencode_conversation"


class LightTool(llm.Tool):
    """A tool that records how it was called."""

    name = "LightOn"
    description = "Turn on a light"
    parameters = probatio.Schema({probatio.Required("name"): str})
    integration = DOMAIN

    def __init__(self) -> None:
        """Initialize the tool."""
        self.calls: list[tuple[dict, llm.LLMContext]] = []

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        """Record the call."""
        self.calls.append((tool_input.tool_args, llm_context))
        return llm.ToolResult(data={"success": True})


class LightAPI(llm.API):
    """An LLM API with one tool."""

    tool: LightTool

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the API's instance."""
        return llm.APIInstance(
            api=self,
            api_prompt="Turn on lights when asked.",
            llm_context=llm_context,
            tools=[self.tool],
        )


@pytest.fixture
def light_tool(hass: HomeAssistant) -> LightTool:
    """Register an LLM API with a light tool."""
    api = LightAPI(hass=hass, id="lights", name="Lights")
    api.tool = LightTool()
    llm.async_register_api(hass, api)
    return api.tool


@pytest.fixture
def mock_config_entry(light_tool: LightTool) -> MockConfigEntry:
    """Return an entry whose agent can use the light tool."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="opencode-server",
        data={
            CONF_URL: URL,
            CONF_TOKEN: "token",
            CONF_RENEWED_AT: dt_util.utcnow().timestamp(),
        },
        subentries_data=[
            {
                "subentry_type": "conversation",
                "data": {
                    CONF_AGENT: "build",
                    CONF_PROMPT: "Be brief.",
                    CONF_LLM_HASS_API: ["lights"],
                    CONF_TIMEOUT: 10,
                },
                "title": "OpenCode conversation",
                "unique_id": None,
            },
        ],
    )


@pytest.mark.usefixtures("init_integration")
async def test_lists_tools(hass: HomeAssistant, mock_opencode: FakeOpenCode) -> None:
    """Test the socket opens and lists the agent's tools."""
    [socket] = mock_opencode.sockets
    assert (socket.name, socket.directory) == ("homeassistant", None)

    reply = await socket.request(0, "initialize", protocolVersion="2025-06-18")
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["capabilities"] == {"tools": {}}

    reply = await socket.request(1, "tools/list")
    assert reply["result"]["tools"] == [
        {
            "name": "LightOn",
            "description": "Turn on a light",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }
    ]

    reply = await socket.request(2, "resources/list")
    assert reply["error"]["code"] == -32601


async def test_lists_admin_tools(hass: HomeAssistant) -> None:
    """Test an API only administrators can use still lists its tools."""
    if "requires_admin" not in {field.name for field in dataclasses.fields(llm.API)}:
        pytest.skip("APIs only need an administrator from Home Assistant 2026.11")
    api = LightAPI(hass=hass, id="admin_lights", name="Lights", requires_admin=True)
    api.tool = LightTool()
    llm.async_register_api(hass, api)
    tools = HomeAssistantTools(hass, MagicMock(), None, ["admin_lights"], {})

    reply = await tools.async_handle("tools/list", {})

    assert [tool["name"] for tool in reply["result"]["tools"]] == ["LightOn"]


@pytest.mark.usefixtures("init_integration")
async def test_tool_calls(
    hass: HomeAssistant, mock_opencode: FakeOpenCode, light_tool: LightTool
) -> None:
    """Test only the agent's own sessions can call the tools."""
    [socket] = mock_opencode.sockets
    # So Assist doesn't say the agent can't control the home
    assert (
        hass.states.get(AGENT_ID).attributes["supported_features"]
        == conversation.ConversationEntityFeature.CONTROL
    )
    mock_opencode.turns = [text_turn("The kitchen light is on.")]

    result = await conversation.async_converse(
        hass, "Turn on the kitchen light", None, Context(), agent_id=AGENT_ID
    )

    assert result.response.speech["plain"]["speech"] == "The kitchen light is on."
    create_kwargs = mock_opencode.async_create_session.call_args.kwargs
    assert create_kwargs["permissions"] == [
        {"action": "question", "resource": "*", "effect": "deny"},
        {"action": "homeassistant_*", "resource": "*", "effect": "allow"},
    ]
    session_id, prompt = mock_opencode.prompts[0]
    assert "Turn on lights when asked." in prompt
    assert TOOLS_PROMPT in prompt

    reply = await socket.request(
        0,
        "tools/call",
        name="LightOn",
        arguments={"name": "kitchen"},
        _meta={SESSION_ID_META: session_id},
    )
    assert reply["result"] == {
        "content": [{"type": "text", "text": '{"success": true}'}],
        "isError": False,
    }
    [(args, llm_context)] = light_tool.calls
    assert args == {"name": "kitchen"}
    assert llm_context.platform == DOMAIN

    reply = await socket.request(
        2,
        "tools/call",
        name="LightOn",
        arguments={"name": "kitchen"},
        _meta={SESSION_ID_META: "ses_someone_else"},
    )
    assert reply["result"] == {
        "content": [{"type": "text", "text": NOT_OUR_SESSION}],
        "isError": True,
    }
    assert len(light_tool.calls) == 1
