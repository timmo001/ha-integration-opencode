"""A small client for the OpenCode server's session API."""

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from typing import Any

import aiohttp
from yarl import URL

from .const import REQUEST_TIMEOUT


class OpenCodeError(Exception):
    """The OpenCode server returned an error."""


class OpenCodeAuthError(OpenCodeError):
    """The OpenCode server refused the token or pairing code."""


class OpenCodeConnectionError(OpenCodeError):
    """The OpenCode server could not be reached."""


class OpenCodeNotFoundError(OpenCodeError):
    """The session or resource no longer exists."""


class OpenCodeConflictError(OpenCodeError):
    """The session can't take the request, such as when it's archived."""


class OpenCodeTurnError(OpenCodeError):
    """The session's turn failed or was interrupted."""


@dataclass(frozen=True, slots=True)
class OpenCodeEvent:
    """An event from the server's event stream."""

    type: str
    data: dict[str, Any]


def parse_pairing_link(link: str) -> tuple[str, str]:
    """Split a pairing link into the server's address and its one-time code."""
    url = URL(link.strip())
    base, separator, code = url.path.partition("/auth/connect/")
    if (
        url.scheme not in ("http", "https")
        or not url.host
        or not separator
        or not code
        or "/" in code
    ):
        raise ValueError(link)
    return str(url.with_path(base).with_query(None).with_fragment(None)).rstrip(
        "/"
    ), code


class OpenCodeClient:
    """Talk to one OpenCode server."""

    def __init__(
        self, session: aiohttp.ClientSession, url: str, token: str | None
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._url = url.rstrip("/")
        self._headers: dict[str, str] = {}
        if token:
            self.set_token(token)

    def set_token(self, token: str) -> None:
        """Use a paired token, which the server takes as its password."""
        basic = base64.b64encode(f"opencode:{token}".encode()).decode()
        self._headers["Authorization"] = f"Basic {basic}"

    async def async_redeem(self, code: str) -> str:
        """Trade a one-time pairing code for a token, without using it yet."""
        result = await self._async_request(
            "GET",
            f"/auth/connect/{code}",
            headers={"Accept": "application/json"},
        )
        return result["token"]

    async def async_renew_token(self) -> str:
        """Pair again with the current token, switch to the new one and return it."""
        code = (await self._async_request("POST", "/api/pair"))["code"]
        token = await self.async_redeem(code)
        self.set_token(token)
        return token

    async def async_get_info(self) -> dict[str, Any]:
        """Return the server's version and identity."""
        return await self._async_request("GET", "/api/info")

    async def async_get_agents(self) -> list[dict[str, Any]]:
        """Return the server's agents."""
        return (await self._async_request("GET", "/api/agent"))["data"]

    async def async_get_models(self) -> list[dict[str, Any]]:
        """Return the models the server can use."""
        return (await self._async_request("GET", "/api/model"))["data"]

    async def async_create_session(
        self,
        *,
        title: str,
        agent: str,
        model: dict[str, str] | None,
        directory: str | None,
        permissions: list[dict[str, str]],
    ) -> str:
        """Create a session and return its ID."""
        payload: dict[str, Any] = {
            "title": title,
            "agent": agent,
            "permissions": permissions,
        }
        if model:
            payload["model"] = model
        if directory:
            payload["location"] = {"directory": directory}
        result = await self._async_request("POST", "/api/session", json=payload)
        return result["data"]["id"]

    async def async_prompt(self, session_id: str, text: str) -> None:
        """Send a prompt to a session."""
        await self._async_request(
            "POST", f"/api/session/{session_id}/prompt", json={"text": text}
        )

    async def async_reject_permission(
        self, session_id: str, request_id: str, message: str
    ) -> None:
        """Reject a permission request the session is waiting on."""
        await self._async_request(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            json={"decision": "reject", "message": message},
        )

    async def async_get_permission_requests(self, session_id: str) -> list[str]:
        """Return the IDs of the permission requests the session is waiting on."""
        result = await self._async_request(
            "GET", f"/api/session/{session_id}/permission"
        )
        return [request["id"] for request in result["data"]]

    async def async_get_forms(self, session_id: str) -> list[str]:
        """Return the IDs of the forms the session is waiting on."""
        result = await self._async_request("GET", f"/api/session/{session_id}/form")
        return [form["id"] for form in result["data"]]

    async def async_cancel_form(self, session_id: str, form_id: str) -> None:
        """Cancel a form the session is waiting on."""
        await self._async_request("DELETE", f"/api/session/{session_id}/form/{form_id}")

    async def async_interrupt(self, session_id: str) -> None:
        """Stop the session's running turn."""
        await self._async_request("POST", f"/api/session/{session_id}/interrupt")

    async def async_archive(self, session_id: str) -> None:
        """Archive a session, keeping its history on the server."""
        await self._async_request(
            "PATCH", f"/api/session/{session_id}", json={"archived": True}
        )

    async def async_wait(self, session_id: str, timeout: float) -> None:
        """Wait until the session's running turn has ended."""
        await self._async_request(
            "POST",
            f"/api/experimental/session/{session_id}/wait",
            timeout=aiohttp.ClientTimeout(total=timeout),
        )

    async def async_get_last_text(self, session_id: str) -> str:
        """Return the text of the session's latest assistant message."""
        result = await self._async_request(
            "GET",
            f"/api/session/{session_id}/message",
            params={"order": "desc", "limit": "20"},
        )
        for message in result["data"]:
            if message["type"] != "assistant":
                continue
            text = "\n\n".join(
                part["text"]
                for part in message.get("content", [])
                if part["type"] == "text" and part["text"]
            )
            if text:
                return text
        return ""

    @asynccontextmanager
    async def async_events(
        self, types: list[str]
    ) -> AsyncIterator[AsyncIterator[OpenCodeEvent]]:
        """Open the event stream, returning once the server is sending events."""
        response = await self._async_send(
            "GET",
            "/api/event",
            params=[("types", event_type) for event_type in types],
            timeout=aiohttp.ClientTimeout(
                total=None, connect=REQUEST_TIMEOUT, sock_read=None
            ),
        )
        try:
            events = _async_read_events(response)
            # The server sends server.connected once it's subscribed, so events
            # for a prompt sent after this can't be missed.
            try:
                await anext(events)
            except StopAsyncIteration as err:
                raise OpenCodeConnectionError("The event stream closed") from err
            yield events
        finally:
            response.close()

    @asynccontextmanager
    async def async_client_mcp(
        self, name: str, directory: str | None
    ) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
        """Open a socket the server uses as an MCP client of ours.

        Sessions in the directory, or the server's default without one, get
        the tools we answer with as `<name>_<tool>`.
        """
        params = {"name": name}
        if directory:
            params["location[directory]"] = directory
        try:
            socket = await self._session.ws_connect(
                f"{self._url}/api/experimental/client-mcp",
                params=params,
                headers=self._headers,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=REQUEST_TIMEOUT),
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise OpenCodeAuthError from err
            raise OpenCodeConnectionError(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OpenCodeConnectionError(str(err)) from err
        try:
            yield socket
        finally:
            await socket.close()

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> Any:
        response = await self._async_send(
            method, path, json=json, params=params, headers=headers, timeout=timeout
        )
        async with response:
            if response.status == 204:
                return None
            try:
                return await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise OpenCodeConnectionError(str(err)) from err

    async def _async_send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> aiohttp.ClientResponse:
        try:
            response = await self._session.request(
                method,
                f"{self._url}{path}",
                headers={**self._headers, **(headers or {})},
                json=json,
                params=params,
                timeout=timeout or aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OpenCodeConnectionError(str(err)) from err
        if response.status < 400:
            return response
        response.release()
        if response.status in (401, 403):
            raise OpenCodeAuthError
        if response.status == 404:
            raise OpenCodeNotFoundError(path)
        if response.status == 409:
            raise OpenCodeConflictError(path)
        raise OpenCodeError(f"{method} {path} returned HTTP {response.status}")


async def _async_read_events(
    response: aiohttp.ClientResponse,
) -> AsyncIterator[OpenCodeEvent]:
    """Parse the server-sent events, one JSON object per data line."""
    try:
        async for raw_line in response.content:
            line = raw_line.decode().strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line.removeprefix("data:"))
            yield OpenCodeEvent(type=event["type"], data=event.get("data") or {})
    except (aiohttp.ClientError, TimeoutError) as err:
        raise OpenCodeConnectionError(str(err)) from err
