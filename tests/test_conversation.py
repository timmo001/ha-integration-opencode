"""Tests for the OpenCode conversation agent."""

from datetime import timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import conversation, persistent_notification
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session, intent, llm
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.opencode.api import (
    OpenCodeConnectionError,
    OpenCodeEvent,
    OpenCodeNotFoundError,
)
from custom_components.opencode.const import (
    FULL_ANSWER_MESSAGE,
    HANDOFF_FAILED_MESSAGE,
    HANDOFF_MESSAGE,
    HANDOFF_TIMEOUT,
    SPEECH_PROMPT,
)
from custom_components.opencode.entity import OpenCodeBaseEntity

from .conftest import FakeOpenCode, slow_turn, text_turn

AGENT_ID = "conversation.opencode_conversation"
SATELLITE_ID = "assist_satellite.kitchen"


async def _ask(
    hass: HomeAssistant,
    text: str,
    conversation_id: str | None = None,
    satellite_id: str | None = None,
) -> conversation.ConversationResult:
    # Satellites send a context without a user
    return await conversation.async_converse(
        hass,
        text,
        conversation_id,
        Context(),
        agent_id=AGENT_ID,
        satellite_id=satellite_id,
    )


@pytest.mark.usefixtures("init_integration")
async def test_answer_and_follow_up(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test the answer streams back and a follow-up reuses the session."""
    mock_opencode.turns = [
        text_turn("Paris", " is the capital."),
        text_turn("About 2 million.", message_id="msg_2"),
    ]

    result = await _ask(hass, "What's the capital of France?")

    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert result.response.speech["plain"]["speech"] == "Paris is the capital."
    session_id, prompt = mock_opencode.prompts[0]
    # The first prompt carries the instructions
    assert prompt.endswith("What's the capital of France?")
    assert prompt != "What's the capital of France?"
    create_kwargs = mock_opencode.async_create_session.call_args.kwargs
    assert create_kwargs["agent"] == "build"
    assert create_kwargs["permissions"] == [
        {"action": "question", "resource": "*", "effect": "deny"}
    ]

    result = await _ask(hass, "How many people live there?", result.conversation_id)

    assert result.response.speech["plain"]["speech"] == "About 2 million."
    assert mock_opencode.prompts[1] == (session_id, "How many people live there?")
    assert mock_opencode.sessions == [session_id]


@pytest.mark.usefixtures("init_integration")
async def test_thinking_and_tool_calls(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test thinking and the tools OpenCode ran show in the chat log."""
    mock_opencode.turns = [
        [
            OpenCodeEvent(
                "session.reasoning.delta",
                {"assistantMessageID": "msg_1", "ordinal": 0, "delta": "Look it up."},
            ),
            OpenCodeEvent(
                "session.tool.input.started",
                {"assistantMessageID": "msg_1", "id": "call_1", "name": "search"},
            ),
            OpenCodeEvent(
                "session.tool.called",
                {
                    "assistantMessageID": "msg_1",
                    "id": "call_1",
                    "input": {"query": "living room"},
                    "executed": True,
                },
            ),
            OpenCodeEvent(
                "session.tool.success",
                {
                    "assistantMessageID": "msg_1",
                    "id": "call_1",
                    "content": [{"type": "text", "text": "21.5 °C"}],
                    "executed": True,
                },
            ),
            *text_turn("It's 21.5 °C.", message_id="msg_2"),
        ]
    ]

    result = await _ask(hass, "How warm is the living room?")

    assert result.response.speech["plain"]["speech"] == "It's 21.5 °C."
    with (
        chat_session.async_get_chat_session(hass, result.conversation_id) as session,
        conversation.async_get_chat_log(hass, session) as chat_log,
    ):
        thinking, tool_result, answer = chat_log.content[-3:]
    assert isinstance(thinking, conversation.AssistantContent)
    assert thinking.thinking_content == "Look it up."
    assert thinking.tool_calls == [
        llm.ToolInput(
            tool_name="search",
            tool_args={"query": "living room"},
            id="call_1",
            external=True,
        )
    ]
    assert isinstance(tool_result, conversation.ToolResultContent)
    assert tool_result.tool_call_id == "call_1"
    assert tool_result.result == llm.ToolResult(data={"content": "21.5 °C"})
    assert isinstance(answer, conversation.AssistantContent)
    assert answer.content == "It's 21.5 °C."


@pytest.mark.usefixtures("init_integration")
async def test_extra_system_prompt(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a caller's extra prompt is sent once, even partway through."""
    mock_opencode.turns = [text_turn("Hi."), text_turn("Yes."), text_turn("No.")]

    result = await _ask(hass, "Hello")
    for text in ("Is the door locked?", "Is the window open?"):
        await conversation.async_converse(
            hass,
            text,
            result.conversation_id,
            Context(),
            agent_id=AGENT_ID,
            extra_system_prompt="The user just got home.",
        )

    assert [prompt for _, prompt in mock_opencode.prompts[1:]] == [
        "The user just got home.\n\nIs the door locked?",
        "Is the window open?",
    ]


@pytest.mark.usefixtures("init_integration")
async def test_gone_session_starts_another(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a follow-up to a session the server lost starts a new one."""
    mock_opencode.turns = [text_turn("One."), text_turn("Two.")]
    result = await _ask(hass, "First")

    prompt = mock_opencode.async_prompt.side_effect

    async def prompt_session_gone(session_id: str, text: str) -> None:
        if session_id == "ses_1":
            raise OpenCodeNotFoundError(session_id)
        await prompt(session_id, text)

    mock_opencode.async_prompt.side_effect = prompt_session_gone
    result = await _ask(hass, "Second", result.conversation_id)

    assert result.response.speech["plain"]["speech"] == "Two."
    assert mock_opencode.sessions == ["ses_1", "ses_2"]
    # The new session gets the instructions again
    assert mock_opencode.prompts[-1][0] == "ses_2"
    assert mock_opencode.prompts[-1][1].endswith("Second")
    assert mock_opencode.prompts[-1][1] != "Second"


@pytest.mark.usefixtures("init_integration")
async def test_permission_and_form_refused(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test nothing waits on an answer nobody can give."""
    mock_opencode.turns = [
        [
            OpenCodeEvent("permission.asked", {"id": "per_1", "action": "bash"}),
            OpenCodeEvent(
                "form.created", {"form": {"id": "frm_1", "sessionID": "ses_1"}}
            ),
            *text_turn("Done without it."),
        ]
    ]

    result = await _ask(hass, "Run something")

    assert result.response.speech["plain"]["speech"] == "Done without it."
    mock_opencode.async_reject_permission.assert_awaited_once()
    assert mock_opencode.async_reject_permission.call_args.args[:2] == (
        "ses_1",
        "per_1",
    )
    mock_opencode.async_cancel_form.assert_awaited_once_with("ses_1", "frm_1")


@pytest.mark.usefixtures("init_integration")
async def test_answer_read_when_not_streamed(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a turn that ends without text deltas reads the last message."""
    mock_opencode.turns = [[OpenCodeEvent("session.execution.succeeded", {})]]
    mock_opencode.last_text = "Read from the messages."

    result = await _ask(hass, "Hello")

    assert result.response.speech["plain"]["speech"] == "Read from the messages."
    mock_opencode.async_get_last_text.assert_awaited_once_with("ses_1")


@pytest.mark.usefixtures("init_integration")
async def test_stream_drop_waits_for_answer(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a dropped event stream waits and finishes from the messages."""
    mock_opencode.turns = [
        [
            OpenCodeEvent(
                "session.text.delta",
                {"assistantMessageID": "msg_1", "ordinal": 0, "delta": "Half"},
            ),
        ]
    ]
    mock_opencode.last_text = "Half and the rest."

    result = await _ask(hass, "Hello")

    assert result.response.speech["plain"]["speech"] == "Half and the rest."
    mock_opencode.async_wait.assert_awaited_once()


@pytest.mark.parametrize(
    "turn",
    [
        pytest.param(
            [
                OpenCodeEvent(
                    "session.execution.failed",
                    {"error": {"type": "x", "message": "no"}},
                )
            ],
            id="failed",
        ),
        pytest.param(
            [OpenCodeEvent("session.execution.interrupted", {"reason": "user"})],
            id="interrupted",
        ),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_turn_failed(
    hass: HomeAssistant, mock_opencode: FakeOpenCode, turn: list[OpenCodeEvent]
) -> None:
    """Test a failed turn answers with a translated error."""
    mock_opencode.turns = [turn]

    result = await _ask(hass, "Hello")

    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert result.response.speech["plain"]["speech"] == (
        "OpenCode couldn't finish the answer, check the session on the server"
    )


@pytest.mark.usefixtures("init_integration")
async def test_unreachable_server(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test an unreachable server answers with a translated error."""
    mock_opencode.async_create_session.side_effect = OpenCodeConnectionError

    result = await _ask(hass, "Hello")

    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert (
        result.response.speech["plain"]["speech"]
        == "Couldn't reach the OpenCode server"
    )


@pytest.mark.usefixtures("init_integration")
async def test_timeout_stops_turn(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a turn still running at the timeout is stopped when typed."""
    mock_opencode.turns = [slow_turn("Let me check.")]

    with patch.object(OpenCodeBaseEntity, "_timeout", 0.05):
        result = await _ask(hass, "Research the weather")

    assert result.response.response_type is intent.IntentResponseType.ERROR
    await hass.async_block_till_done()
    mock_opencode.async_interrupt.assert_awaited_once_with("ses_1")


@pytest.mark.usefixtures("init_integration")
async def test_slow_answer_announced(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a satellite hears the answer later, and a follow-up starts afresh."""
    announce = async_mock_service(hass, "assist_satellite", "announce")
    mock_opencode.turns = [slow_turn("Let me check."), text_turn("Hi.")]
    mock_opencode.last_text = "It's 21 degrees."

    with patch.object(OpenCodeBaseEntity, "_timeout", 0.05):
        result = await _ask(hass, "Research the weather", satellite_id=SATELLITE_ID)

    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert result.response.speech["plain"]["speech"] == HANDOFF_MESSAGE
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_opencode.async_interrupt.assert_not_awaited()
    mock_opencode.async_wait.assert_awaited_once_with("ses_1", HANDOFF_TIMEOUT)
    assert len(announce) == 1
    assert announce[0].data == {
        "entity_id": SATELLITE_ID,
        "message": "It's 21 degrees.",
    }
    mock_opencode.async_archive.assert_awaited_once_with("ses_1")

    await _ask(hass, "Hello", result.conversation_id, satellite_id=SATELLITE_ID)
    assert mock_opencode.sessions == ["ses_1", "ses_2"]


@pytest.mark.usefixtures("init_integration")
async def test_slow_answer_failed(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a satellite hears an apology when the answer never comes."""
    announce = async_mock_service(hass, "assist_satellite", "announce")
    mock_opencode.turns = [slow_turn()]
    mock_opencode.async_wait.side_effect = OpenCodeConnectionError

    with patch.object(OpenCodeBaseEntity, "_timeout", 0.05):
        await _ask(hass, "Research the weather", satellite_id=SATELLITE_ID)
    await hass.async_block_till_done(wait_background_tasks=True)

    mock_opencode.async_interrupt.assert_awaited_once_with("ses_1")
    assert announce[0].data["message"] == HANDOFF_FAILED_MESSAGE


@pytest.mark.usefixtures("init_integration")
async def test_spoken_answer_plain(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a satellite hears plain text, and the full answer is saved."""
    mock_opencode.turns = [
        text_turn(
            "## Weather\nIt's **21 degrees**, see [the forecast](https://",
            "example.com).\n[full answer]\n| Day | High |\n| --- | --- |",
        )
    ]

    result = await _ask(hass, "What's the weather?", satellite_id=SATELLITE_ID)

    assert result.response.speech["plain"]["speech"] == (
        f"Weather.\nIt's 21 degrees, see the forecast.\n{FULL_ANSWER_MESSAGE}"
    )
    assert SPEECH_PROMPT in mock_opencode.prompts[0][1]
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    (notification,) = notifications.values()
    assert notification["title"] == "OpenCode conversation"
    assert notification["message"].startswith("> What's the weather?\n\n## Weather")
    assert "[full answer]" not in notification["message"]
    assert "| Day | High |" in notification["message"]


@pytest.mark.usefixtures("init_integration")
async def test_typed_answer_as_written(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a typed question gets the answer as written."""
    mock_opencode.turns = [text_turn("It's **21 degrees**.")]

    result = await _ask(hass, "What's the weather?")

    assert result.response.speech["plain"]["speech"] == "It's **21 degrees**."
    assert SPEECH_PROMPT not in mock_opencode.prompts[0][1]
    assert not persistent_notification._async_get_or_create_notifications(hass)


@pytest.mark.usefixtures("init_integration")
async def test_session_archived_on_cleanup(
    hass: HomeAssistant,
    mock_opencode: FakeOpenCode,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the session is archived when Assist forgets the conversation."""
    mock_opencode.turns = [text_turn("Hi.")]
    await _ask(hass, "Hello")

    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_opencode.async_archive.assert_awaited_once_with("ses_1")


async def test_sessions_archived_on_unload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_opencode: FakeOpenCode,
) -> None:
    """Test open sessions are archived when the entry unloads."""
    mock_opencode.turns = [text_turn("Hi.")]
    await _ask(hass, "Hello")

    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    mock_opencode.async_archive.assert_awaited_once_with("ses_1")
