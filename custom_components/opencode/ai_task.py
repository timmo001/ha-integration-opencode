"""The AI Task platform for the OpenCode integration."""

from functools import partial
import json
from json import JSONDecodeError
from typing import override

from homeassistant.components import ai_task, conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.json import json_loads
import probatio

from . import OpenCodeConfigEntry
from .api import OpenCodeError
from .const import DOMAIN, LOGGER
from .entity import OpenCodeBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OpenCodeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AI Task entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "ai_task_data":
            continue

        async_add_entities(
            [OpenCodeTaskEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class OpenCodeTaskEntity(ai_task.AITaskEntity, OpenCodeBaseEntity):
    """OpenCode doing tasks for automations and scripts."""

    _attr_supported_features = ai_task.AITaskEntityFeature.GENERATE_DATA

    @override
    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Run the task in a session of its own."""
        prompt = f"{chat_log.content[0].content}\n\n{task.instructions}"
        if task.structure:
            schema = probatio.to_openapi(
                task.structure,
                custom_serializer=(
                    chat_log.llm_api.custom_serializer
                    if chat_log.llm_api
                    else llm.selector_serializer
                ),
                openapi_version="3.1.0",
            )
            prompt += (
                "\n\nAnswer with only a JSON object matching this JSON schema, "
                f"and nothing else:\n{json.dumps(schema)}"
            )

        try:
            session_id = await self._async_create_session(task.name)
        except OpenCodeError as err:
            LOGGER.error("Error talking to OpenCode: %s", err)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            ) from err
        try:
            await self._async_answer(
                chat_log, partial(self._async_send, session_id, prompt)
            )
        finally:
            self._async_archive(session_id)

        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_answer"
            )
        text = chat_log.content[-1].content or ""
        if not task.structure:
            return ai_task.GenDataTaskResult(
                conversation_id=chat_log.conversation_id, data=text
            )

        try:
            data = json_loads(_strip_code_fence(text))
        except JSONDecodeError as err:
            LOGGER.debug("OpenCode's answer wasn't JSON: %s", text)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_structure"
            ) from err
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=data
        )

    async def _async_send(self, session_id: str, prompt: str) -> str:
        await self._client.async_prompt(session_id, prompt)
        return session_id


def _strip_code_fence(text: str) -> str:
    """Return the text inside a Markdown code block, if it's wrapped in one."""
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.removeprefix("```").removesuffix("```")
        # Drop the language, such as json
        text = text.partition("\n")[2]
    return text
