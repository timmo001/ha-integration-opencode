"""The conversation platform for the OpenCode integration."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from typing import Any, Literal, override

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_MODEL, CONF_PROMPT, CONF_TIMEOUT, MATCH_ALL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import chat_session, device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenCodeConfigEntry
from .api import (
    OpenCodeClient,
    OpenCodeConflictError,
    OpenCodeConnectionError,
    OpenCodeError,
    OpenCodeEvent,
    OpenCodeNotFoundError,
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
    LOGGER,
    PERMISSION_REJECT_MESSAGE,
)

# Nobody can answer the question tool from a voice assistant
SESSION_PERMISSIONS = [{"action": "question", "resource": "*", "effect": "deny"}]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OpenCodeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up conversation entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue

        async_add_entities(
            [OpenCodeConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class OpenCodeConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """An OpenCode agent answering Assist."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: OpenCodeConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the agent."""
        self.entry = entry
        self.subentry = subentry
        self._client: OpenCodeClient = entry.runtime_data.client
        # Assist conversation ID to OpenCode session ID
        self._sessions: dict[str, str] = {}
        # The extra system prompt each session was last given
        self._extra_prompts: dict[str, str | None] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="OpenCode",
            model=subentry.data.get(CONF_MODEL) or subentry.data[CONF_AGENT],
            sw_version=entry.runtime_data.version,
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @override
    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Archive the open sessions when the entity goes."""
        conversation.async_unset_agent(self.hass, self.entry)
        for session_id in self._sessions.values():
            self._async_archive(session_id)
        self._sessions.clear()
        await super().async_will_remove_from_hass()

    @property
    @override
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    @override
    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Send the message to OpenCode and stream the answer back."""
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                None,
                self.subentry.data.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        conversation_id = chat_log.conversation_id
        if session := chat_session.current_session.get():
            # Register once, when this agent first sees the conversation
            if conversation_id not in self._locks:
                session.async_on_cleanup(
                    callback(lambda: self._async_forget(conversation_id))
                )

        async with self._locks[conversation_id]:
            instructions = chat_log.content[0].content
            stream = self._async_stream(
                conversation_id,
                instructions,
                chat_log.extra_system_prompt,
                user_input.text,
            )
            try:
                async with asyncio.timeout(
                    self.subentry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                ):
                    async for _ in chat_log.async_add_delta_content_stream(
                        self.entity_id, stream
                    ):
                        pass
            except TimeoutError as err:
                if session_id := self._sessions.get(conversation_id):
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

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_stream(
        self,
        conversation_id: str,
        instructions: str,
        extra_system_prompt: str | None,
        text: str,
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Prompt the session and turn its events into chat log deltas."""
        async with self._client.async_events(list(EVENT_TYPES)) as events:
            session_id = await self._async_prompt(
                conversation_id, instructions, extra_system_prompt, text
            )
            message_id: str | None = None
            streamed = ""
            finished = False
            try:
                async for event in _async_session_events(events, session_id):
                    match event.type:
                        case "session.text.delta":
                            if event.data["assistantMessageID"] != message_id:
                                message_id = event.data["assistantMessageID"]
                                streamed = ""
                                yield {"role": "assistant"}
                            streamed += event.data["delta"]
                            yield {"content": event.data["delta"]}
                        case "permission.asked":
                            await self._client.async_reject_permission(
                                session_id, event.data["id"], PERMISSION_REJECT_MESSAGE
                            )
                        case "form.created":
                            await self._client.async_cancel_form(
                                session_id, event.data["form"]["id"]
                            )
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
                await self._client.async_wait(
                    session_id, self.subentry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                )
            elif message_id is not None:
                return

        # Nothing streamed, or the stream dropped: read the last answer
        answer = await self._client.async_get_last_text(session_id)
        if message_id is not None and answer.startswith(streamed):
            yield {"content": answer.removeprefix(streamed)}
        else:
            yield {"role": "assistant"}
            yield {"content": answer}

    async def _async_prompt(
        self,
        conversation_id: str,
        instructions: str,
        extra_system_prompt: str | None,
        text: str,
    ) -> str:
        """Send the prompt, starting a session when there isn't a usable one."""
        if session_id := self._sessions.get(conversation_id):
            prompt = text
            if extra_system_prompt and (
                extra_system_prompt != self._extra_prompts.get(conversation_id)
            ):
                # A caller, such as an automation, added a prompt mid-conversation
                prompt = f"{extra_system_prompt}\n\n{text}"
            try:
                await self._client.async_prompt(session_id, prompt)
            except OpenCodeNotFoundError, OpenCodeConflictError:
                LOGGER.debug("Session %s is gone, starting another", session_id)
            else:
                self._extra_prompts[conversation_id] = extra_system_prompt
                return session_id

        session_id = await self._async_create_session()
        self._sessions[conversation_id] = session_id
        await self._client.async_prompt(session_id, f"{instructions}\n\n{text}")
        self._extra_prompts[conversation_id] = extra_system_prompt
        return session_id

    async def _async_create_session(self) -> str:
        """Create a session with this agent's options."""
        options = self.subentry.data
        model = None
        if model_ref := options.get(CONF_MODEL):
            provider_id, _, model_id = model_ref.partition("/")
            model = {"providerID": provider_id, "id": model_id}
            if variant := options.get(CONF_VARIANT):
                model["variant"] = variant
        return await self._client.async_create_session(
            title=f"Home Assistant: {self.subentry.title}",
            agent=options.get(CONF_AGENT, DEFAULT_AGENT),
            model=model,
            directory=options.get(CONF_DIRECTORY),
            permissions=SESSION_PERMISSIONS,
        )

    @callback
    def _async_forget(self, conversation_id: str) -> None:
        """Archive the session when Assist forgets the conversation."""
        self._locks.pop(conversation_id, None)
        self._extra_prompts.pop(conversation_id, None)
        if session_id := self._sessions.pop(conversation_id, None):
            self._async_archive(session_id)

    @callback
    def _async_archive(self, session_id: str) -> None:
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


async def _async_ignore_errors(coro: Coroutine[Any, Any, None]) -> None:
    """Run housekeeping that may fail without affecting the user."""
    try:
        await coro
    except OpenCodeError as err:
        LOGGER.debug("OpenCode housekeeping failed: %s", err)
