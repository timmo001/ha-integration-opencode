"""The conversation platform for the OpenCode integration."""

import asyncio
from collections import defaultdict
from functools import partial
from typing import Literal, override

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT, MATCH_ALL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import chat_session, llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenCodeConfigEntry
from .api import OpenCodeConflictError, OpenCodeError, OpenCodeNotFoundError
from .const import (
    DOMAIN,
    HANDOFF_FAILED_MESSAGE,
    HANDOFF_TIMEOUT,
    LOGGER,
    SPEECH_PROMPT,
    TOOLS_PROMPT,
)
from .entity import OpenCodeBaseEntity


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
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
    OpenCodeBaseEntity,
):
    """An OpenCode agent answering Assist."""

    _attr_supports_streaming = True

    def __init__(self, entry: OpenCodeConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the agent."""
        super().__init__(entry, subentry)
        # Assist conversation ID to OpenCode session ID
        self._sessions: dict[str, str] = {}
        # The extra system prompt each session was last given
        self._extra_prompts: dict[str, str | None] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

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
                self.subentry.data.get(CONF_LLM_HASS_API),
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

        satellite_id = user_input.satellite_id
        async with self._locks[conversation_id]:
            await self._async_answer(
                chat_log,
                partial(
                    self._async_prompt,
                    conversation_id,
                    chat_log.content[0].content,
                    chat_log.extra_system_prompt,
                    user_input.text,
                    chat_log.llm_api,
                    spoken=satellite_id is not None,
                ),
                (
                    partial(
                        self._async_hand_off,
                        conversation_id,
                        satellite_id,
                        user_input.text,
                    )
                    if satellite_id
                    else None
                ),
                user_input.text if satellite_id else None,
            )

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_prompt(
        self,
        conversation_id: str,
        instructions: str,
        extra_system_prompt: str | None,
        text: str,
        llm_api: llm.APIInstance | None,
        *,
        spoken: bool,
    ) -> str:
        """Send the prompt, starting a session when there isn't a usable one."""
        if session_id := self._sessions.get(conversation_id):
            prompt = text
            if extra_system_prompt and (
                extra_system_prompt != self._extra_prompts.get(conversation_id)
            ):
                # A caller, such as an automation, added a prompt mid-conversation
                prompt = f"{extra_system_prompt}\n\n{text}"
            if llm_api:
                self._async_use_tools(session_id, llm_api)
            try:
                await self._client.async_prompt(session_id, prompt)
            except OpenCodeNotFoundError, OpenCodeConflictError:
                LOGGER.debug("Session %s is gone, starting another", session_id)
                self.entry.runtime_data.tool_sessions.pop(session_id, None)
            else:
                self._extra_prompts[conversation_id] = extra_system_prompt
                return session_id

        session_id = await self._async_create_session(
            self.subentry.title, tools=llm_api is not None
        )
        self._sessions[conversation_id] = session_id
        if llm_api:
            self._async_use_tools(session_id, llm_api)
            instructions = f"{instructions}\n\n{TOOLS_PROMPT}"
        if spoken:
            instructions = f"{instructions}\n\n{SPEECH_PROMPT}"
        await self._client.async_prompt(session_id, f"{instructions}\n\n{text}")
        self._extra_prompts[conversation_id] = extra_system_prompt
        return session_id

    @callback
    def _async_hand_off(
        self, conversation_id: str, satellite_id: str, question: str, session_id: str
    ) -> None:
        """Let a slow turn finish by itself, then announce its answer."""
        # A follow-up starts another session rather than waiting on this one
        self._sessions.pop(conversation_id, None)
        self._extra_prompts.pop(conversation_id, None)
        self.hass.async_create_background_task(
            self._async_announce_later(session_id, satellite_id, question),
            f"{DOMAIN} answer {session_id} later",
        )

    async def _async_announce_later(
        self, session_id: str, satellite_id: str, question: str
    ) -> None:
        """Wait for the turn to end and announce its answer on the satellite."""
        try:
            await self._client.async_wait(session_id, HANDOFF_TIMEOUT)
            answer = await self._client.async_get_last_text(session_id)
        except OpenCodeError as err:
            LOGGER.warning("OpenCode didn't finish answering later: %s", err)
            self._async_interrupt(session_id)
            answer = ""
        self._async_archive(session_id)
        message = (answer and self._speech(question, answer)) or HANDOFF_FAILED_MESSAGE
        try:
            await self.hass.services.async_call(
                "assist_satellite",
                "announce",
                {"message": message},
                target={"entity_id": satellite_id},
                blocking=True,
            )
        except HomeAssistantError as err:
            LOGGER.warning("Couldn't announce OpenCode's answer: %s", err)

    @callback
    def _async_forget(self, conversation_id: str) -> None:
        """Archive the session when Assist forgets the conversation."""
        self._locks.pop(conversation_id, None)
        self._extra_prompts.pop(conversation_id, None)
        if session_id := self._sessions.pop(conversation_id, None):
            self._async_archive(session_id)
