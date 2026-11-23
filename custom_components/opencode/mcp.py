"""Home Assistant's tools for OpenCode sessions, over OpenCode's client MCP socket.

The integration opens a WebSocket to the server, and the server uses it as an
MCP client, offering the tools to every session in the socket's directory.
Each call names the session that made it, so a call is answered with the API
instance of that session's latest turn, and refused for any session this entry
didn't start.
"""

import asyncio
from collections.abc import Callable
import json
from typing import Any

import aiohttp
from homeassistant.components import conversation
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
import probatio

from .api import OpenCodeClient, OpenCodeError
from .const import DOMAIN, LOGGER, MCP_SERVER_NAME

SESSION_ID_META = "ai.opencode/sessionID"
PROTOCOL_VERSION = "2025-11-25"
RECONNECT_MIN = 1
RECONNECT_MAX = 60
NOT_OUR_SESSION = "Only sessions Home Assistant started can use Home Assistant's tools."


class HomeAssistantTools:
    """Serve Home Assistant's tools to the sessions in one directory."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OpenCodeClient,
        directory: str | None,
        api_ids: list[str],
        sessions: dict[str, llm.APIInstance],
    ) -> None:
        """Initialize the tools."""
        self._hass = hass
        self._client = client
        self._directory = directory
        self._api_ids = api_ids
        # Each session this entry started, with its latest turn's API instance
        self._sessions = sessions
        self._tasks: set[asyncio.Task[None]] = set()

    async def async_run(self) -> None:
        """Keep the socket open until cancelled, reconnecting with backoff."""
        delay = RECONNECT_MIN
        while True:
            try:
                async with self._client.async_client_mcp(
                    MCP_SERVER_NAME, self._directory
                ) as socket:
                    async for message in socket:
                        if message.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        if '"notifications/initialized"' in message.data:
                            # Attached, so a later drop starts backing off afresh
                            delay = RECONNECT_MIN
                        # Tool calls can be slow, so each is answered on its own
                        task = self._hass.async_create_background_task(
                            self._async_reply(socket, message.data),
                            f"{DOMAIN} tool request",
                        )
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
            except OpenCodeError as err:
                LOGGER.debug("Home Assistant's tools socket failed: %s", err)
            finally:
                for task in list(self._tasks):
                    task.cancel()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _async_reply(
        self, socket: aiohttp.ClientWebSocketResponse, data: str
    ) -> None:
        """Answer one JSON-RPC request; notifications and responses need none."""
        try:
            message = json.loads(data)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(request_id, str | int) or not isinstance(method, str):
            return
        reply = await self.async_handle(method, message.get("params") or {})
        if not socket.closed:
            await socket.send_json({"jsonrpc": "2.0", "id": request_id, **reply})

    async def async_handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Return the result or error for an MCP request."""
        match method:
            case "initialize":
                return {
                    "result": {
                        # The server only offers versions it supports
                        "protocolVersion": params.get("protocolVersion")
                        or PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "home-assistant", "version": HA_VERSION},
                    }
                }
            case "ping":
                return {"result": {}}
            case "tools/list":
                return {"result": {"tools": await self._async_list_tools()}}
            case "tools/call":
                return {"result": await self._async_call_tool(params)}
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}

    async def _async_list_tools(self) -> list[dict[str, Any]]:
        """List the tools of every API an agent in this directory uses."""
        try:
            api = await llm.async_get_api(
                self._hass,
                self._api_ids,
                llm.LLMContext(
                    platform=DOMAIN,
                    context=Context(),
                    language=self._hass.config.language,
                    assistant=conversation.DOMAIN,
                    device_id=None,
                ),
            )
        except HomeAssistantError as err:
            LOGGER.warning("Couldn't list Home Assistant's tools: %s", err)
            return []
        return [_format_tool(tool, api.custom_serializer) for tool in api.tools]

    async def _async_call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        """Call a tool for the session that asked, if it's one of ours."""
        session_id = (params.get("_meta") or {}).get(SESSION_ID_META)
        if (api := self._sessions.get(session_id)) is None:
            return _call_result(NOT_OUR_SESSION, error=True)
        tool_input = llm.ToolInput(
            tool_name=params.get("name", ""), tool_args=params.get("arguments") or {}
        )
        LOGGER.debug("Tool call: %s(%s)", tool_input.tool_name, tool_input.tool_args)
        try:
            result = await api.async_call_tool(tool_input)
        except (HomeAssistantError, probatio.Invalid) as err:
            return _call_result(f"Error calling tool: {err}", error=True)
        return _call_result(
            json.dumps(result.data, ensure_ascii=False), error=result.error
        )


def _format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Describe a tool the way MCP lists it."""
    schema = probatio.to_openapi(
        tool.parameters, custom_serializer=custom_serializer, openapi_version="3.1.0"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": schema.get("properties", {}),
    }
    if required := schema.get("required"):
        input_schema["required"] = required
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": input_schema,
    }


def _call_result(text: str, *, error: bool) -> dict[str, Any]:
    """Return an MCP tool call result."""
    return {"content": [{"type": "text", "text": text}], "isError": error}
