"""Base entity for the OpenCode integration."""

import asyncio
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
)
from typing import Any

from homeassistant.components import conversation, persistent_notification
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_MODEL, CONF_TIMEOUT
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity

from . import OpenCodeConfigEntry
from .api import (
    OpenCodeClient,
    OpenCodeConnectionError,
    OpenCodeError,
    OpenCodeEvent,
    OpenCodeTurnError,
)
from .const import (
    CONF_AGENT,
    CONF_DIRECTORY,
    CONF_VARIANT,
    DEFAULT_AGENT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    EVENT_TYPES,
    FULL_ANSWER_MESSAGE,
    HANDOFF_MESSAGE,
    LOGGER,
    MCP_SERVER_NAME,
    PERMISSION_REJECT_MESSAGE,
    REQUEST_EVENT_TYPES,
)
from .speech import SpeechCleaner, full_answer, to_speech

# Nobody can answer the question tool from a voice assistant or an automation
SESSION_PERMISSIONS = [{"action": "question", "resource": "*", "effect": "deny"}]
# Home Assistant's own tools, which it checks for itself, need no asking
TOOLS_PERMISSION = {
    "action": f"{MCP_SERVER_NAME}_*",
    "resource": "*",
    "effect": "allow",
}


class OpenCodeBaseEntity(Entity):
    """An entity that answers through OpenCode sessions."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: OpenCodeConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._client: OpenCodeClient = entry.runtime_data.client
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="OpenCode",
            model=subentry.data.get(CONF_MODEL) or subentry.data[CONF_AGENT],
            sw_version=entry.runtime_data.version,
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def _timeout(self) -> float:
        """Return how long to wait for an answer."""
        return self.subentry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

    async def _async_create_session(self, title: str, *, tools: bool = False) -> str:
        """Create a session with this entity's options.

        With `tools`, it can use Home Assistant's tools without asking.
        """
        options = self.subentry.data
        model = None
        if model_ref := options.get(CONF_MODEL):
            provider_id, _, model_id = model_ref.partition("/")
            model = {"providerID": provider_id, "id": model_id}
            if variant := options.get(CONF_VARIANT):
                model["variant"] = variant
        return await self._client.async_create_session(
            title=f"Home Assistant: {title}",
            agent=options.get(CONF_AGENT, DEFAULT_AGENT),
            model=model,
            directory=options.get(CONF_DIRECTORY),
            permissions=[*SESSION_PERMISSIONS, TOOLS_PERMISSION]
            if tools
            else SESSION_PERMISSIONS,
        )

    @callback
    def _async_use_tools(self, session_id: str, llm_api: llm.APIInstance) -> None:
        """Answer the session's tool calls with this turn's API."""
        self.entry.runtime_data.tool_sessions[session_id] = llm_api

    async def _async_answer(
        self,
        chat_log: conversation.ChatLog,
        send: Callable[[], Awaitable[str]],
        hand_off: Callable[[str], None] | None = None,
        spoken_question: str | None = None,
    ) -> None:
        """Send a prompt and stream the answer into the chat log.

        `send` sends the prompt and returns the session it went to. Given
        `hand_off`, a turn still running at the timeout is passed to it to
        finish later, and the answer says so, rather than being stopped.
        Given the question it answers aloud, the answer is cleaned for a
        speaker, and saved in full when that leaves something out.
        """
        session_id: str | None = None

        async def async_send() -> str:
            nonlocal session_id
            session_id = await send()
            return session_id

        async def async_stream() -> AsyncGenerator[
            conversation.AssistantContentDeltaDict
        ]:
            stream = self._async_stream(async_send)
            if spoken_question is not None:
                stream = self._async_speak(spoken_question, stream)
            deadline = asyncio.get_running_loop().time() + self._timeout
            while True:
                try:
                    async with asyncio.timeout_at(deadline):
                        delta = await anext(stream)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    if hand_off is None or session_id is None:
                        raise
                    hand_off(session_id)
                    # Through the stream, so it's spoken even once speech started
                    yield {"role": "assistant"}
                    yield {"content": HANDOFF_MESSAGE}
                    return
                yield delta

        try:
            async for _ in chat_log.async_add_delta_content_stream(
                self.entity_id, async_stream()
            ):
                pass
        except TimeoutError as err:
            if session_id is not None:
                self._async_interrupt(session_id)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="timeout"
            ) from err
        except OpenCodeTurnError as err:
            LOGGER.warning("OpenCode couldn't answer: %s", err)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="turn_failed"
            ) from err
        except OpenCodeError as err:
            LOGGER.error("Error talking to OpenCode: %s", err)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            ) from err

    async def _async_stream(
        self, send: Callable[[], Awaitable[str]]
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Prompt a session and turn its events into chat log deltas."""
        async with self._client.async_events(list(EVENT_TYPES)) as events:
            session_id = await send()
            # The chat log message being streamed, cleared after a tool result
            message_id: str | None = None
            # The message whose text was streamed, and that text
            text_id: str | None = None
            streamed = ""
            tool_names: dict[str, str] = {}
            finished = False
            try:
                async for event in _async_session_events(events, session_id):
                    match event.type:
                        case (
                            "session.text.delta"
                            | "session.reasoning.delta"
                            | "session.tool.called"
                        ):
                            if event.data["assistantMessageID"] != message_id:
                                message_id = event.data["assistantMessageID"]
                                yield {"role": "assistant"}
                            if event.type == "session.text.delta":
                                if text_id != message_id:
                                    text_id = message_id
                                    streamed = ""
                                streamed += event.data["delta"]
                                yield {"content": event.data["delta"]}
                            elif event.type == "session.reasoning.delta":
                                yield {"thinking_content": event.data["delta"]}
                            else:
                                yield {
                                    "tool_calls": [
                                        llm.ToolInput(
                                            tool_name=tool_names.get(
                                                event.data["id"], "tool"
                                            ),
                                            tool_args=event.data["input"],
                                            id=event.data["id"],
                                            external=True,
                                        )
                                    ]
                                }
                        case "session.tool.input.started":
                            tool_names[event.data["id"]] = event.data["name"]
                        case "session.tool.success" | "session.tool.failed":
                            yield {
                                "role": "tool_result",
                                "tool_call_id": event.data["id"],
                                "tool_name": tool_names.get(event.data["id"], "tool"),
                                "result": _tool_result(event.data),
                            }
                            # Whatever follows needs a new assistant message
                            message_id = None
                        case "permission.asked" | "form.created":
                            await self._async_refuse(session_id, event)
                        case "session.execution.succeeded":
                            finished = True
                            break
                        case "session.execution.failed":
                            raise OpenCodeTurnError(event.data["error"]["message"])
                        case "session.execution.interrupted":
                            raise OpenCodeTurnError(
                                f"Interrupted: {event.data['reason']}"
                            )
            except OpenCodeConnectionError as err:
                LOGGER.debug("Event stream for %s failed: %s", session_id, err)

            if not finished:
                # The stream dropped mid-turn: wait for the turn to end instead
                await self._async_wait(session_id, self._timeout)
            elif text_id is not None:
                return

        # Nothing streamed, or the stream dropped: read the last answer
        answer = await self._client.async_get_last_text(session_id)
        if (
            text_id is not None
            and text_id == message_id
            and answer.startswith(streamed)
        ):
            yield {"content": answer.removeprefix(streamed)}
        else:
            yield {"role": "assistant"}
            yield {"content": answer}

    async def _async_refuse(self, session_id: str, event: OpenCodeEvent) -> None:
        """Refuse a permission request or form, since nobody can answer it."""
        if event.type == "permission.asked":
            await self._client.async_reject_permission(
                session_id, event.data["id"], PERMISSION_REJECT_MESSAGE
            )
        else:
            await self._client.async_cancel_form(session_id, event.data["form"]["id"])

    async def _async_wait(self, session_id: str, timeout: float) -> None:
        """Wait for the session's turn to end, refusing what it asks meanwhile."""
        refusing = self.hass.async_create_background_task(
            self._async_refuse_requests(session_id), f"{DOMAIN} refuse {session_id}"
        )
        try:
            await self._client.async_wait(session_id, timeout)
        finally:
            refusing.cancel()

    async def _async_refuse_requests(self, session_id: str) -> None:
        """Refuse every permission request and form until cancelled."""
        try:
            async with self._client.async_events(list(REQUEST_EVENT_TYPES)) as events:
                # Once listening, refuse what was asked before, so nothing slips by
                for request_id in await self._client.async_get_permission_requests(
                    session_id
                ):
                    await self._client.async_reject_permission(
                        session_id, request_id, PERMISSION_REJECT_MESSAGE
                    )
                for form_id in await self._client.async_get_forms(session_id):
                    await self._client.async_cancel_form(session_id, form_id)
                async for event in _async_session_events(events, session_id):
                    await self._async_refuse(session_id, event)
        except OpenCodeError as err:
            LOGGER.debug("Couldn't refuse requests for %s: %s", session_id, err)

    async def _async_speak(
        self,
        question: str,
        stream: AsyncIterator[conversation.AssistantContentDeltaDict],
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Clean a streamed answer for a speaker."""
        cleaner = SpeechCleaner()
        async for delta in stream:
            if "role" in delta:
                # Another message: only the last one is the answer
                if rest := cleaner.flush():
                    yield {"content": rest}
                cleaner = SpeechCleaner()
                yield delta
            elif "content" not in delta:
                # Thinking and tool calls are shown, not spoken
                yield delta
            elif text := cleaner.feed(delta["content"]):
                yield {"content": text}

        rest = cleaner.flush()
        if cleaner.has_more:
            self._async_save_full_answer(question, cleaner.text)
            rest += cleaner.say(FULL_ANSWER_MESSAGE)
        if rest:
            yield {"content": rest}

    def _speech(self, question: str, answer: str) -> str:
        """Return an answer to speak, saving it in full when that loses some."""
        speech, has_more = to_speech(answer)
        if has_more:
            self._async_save_full_answer(question, answer)
            speech = f"{speech}\n{FULL_ANSWER_MESSAGE}".strip()
        return speech

    @callback
    def _async_save_full_answer(self, question: str, answer: str) -> None:
        """Save an answer as written where it can be read later."""
        persistent_notification.async_create(
            self.hass, f"> {question}\n\n{full_answer(answer)}", self.subentry.title
        )

    @callback
    def _async_archive(self, session_id: str) -> None:
        self.entry.runtime_data.tool_sessions.pop(session_id, None)
        # Not tied to the entry, so unloading doesn't cancel it
        self.hass.async_create_background_task(
            _async_ignore_errors(self._client.async_archive(session_id)),
            f"{DOMAIN} archive {session_id}",
        )

    @callback
    def _async_interrupt(self, session_id: str) -> None:
        self.hass.async_create_background_task(
            _async_ignore_errors(self._client.async_interrupt(session_id)),
            f"{DOMAIN} interrupt {session_id}",
        )


async def _async_session_events(
    events: AsyncIterator[OpenCodeEvent], session_id: str
) -> AsyncIterator[OpenCodeEvent]:
    """Yield the events for one session."""
    async for event in events:
        if event.type == "form.created":
            if event.data["form"]["sessionID"] == session_id:
                yield event
        elif event.data.get("sessionID") == session_id:
            yield event


def _tool_result(data: dict[str, Any]) -> llm.ToolResult:
    """Turn a finished OpenCode tool call into a chat log tool result."""
    text = "\n".join(
        part["text"] for part in data.get("content") or [] if part["type"] == "text"
    )
    if error := data.get("error"):
        return llm.ToolResult(
            data={"error": error["type"], "error_text": error["message"]}, error=True
        )
    return llm.ToolResult(data={"content": text})


async def _async_ignore_errors(coro: Coroutine[Any, Any, None]) -> None:
    """Run housekeeping that may fail without affecting the user."""
    try:
        await coro
    except OpenCodeError as err:
        LOGGER.debug("OpenCode housekeeping failed: %s", err)
