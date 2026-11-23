"""Tests for the OpenCode AI Task platform."""

from homeassistant.components import ai_task
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import probatio
import pytest

from custom_components.opencode.api import OpenCodeConnectionError

from .conftest import FakeOpenCode, text_turn

ENTITY_ID = "ai_task.opencode_ai_task"


@pytest.mark.usefixtures("init_integration")
async def test_generate_data(hass: HomeAssistant, mock_opencode: FakeOpenCode) -> None:
    """Test a task runs in a session of its own, archived after."""
    mock_opencode.turns = [text_turn("It's ", "sunny.")]

    result = await ai_task.async_generate_data(
        hass,
        task_name="Weather",
        entity_id=ENTITY_ID,
        instructions="What's the weather like?",
    )

    assert result.data == "It's sunny."
    mock_opencode.async_create_session.assert_awaited_once()
    assert (
        mock_opencode.async_create_session.call_args.kwargs["title"]
        == "Home Assistant: Weather"
    )
    [(session_id, prompt)] = mock_opencode.prompts
    assert prompt.endswith("\n\nWhat's the weather like?")
    await hass.async_block_till_done()
    mock_opencode.async_archive.assert_awaited_once_with(session_id)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param('{"rooms": ["Kitchen"], "count": 1}', id="plain"),
        pytest.param('```json\n{"rooms": ["Kitchen"], "count": 1}\n```', id="fenced"),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_generate_structured_data(
    hass: HomeAssistant, mock_opencode: FakeOpenCode, answer: str
) -> None:
    """Test a structured task asks for JSON and reads it back."""
    mock_opencode.turns = [text_turn(answer)]

    result = await ai_task.async_generate_data(
        hass,
        task_name="Rooms",
        entity_id=ENTITY_ID,
        instructions="Which rooms are warm?",
        structure=probatio.Schema(
            {
                probatio.Required("rooms"): [str],
                probatio.Required("count"): int,
            }
        ),
    )

    assert result.data == {"rooms": ["Kitchen"], "count": 1}
    [(_, prompt)] = mock_opencode.prompts
    assert "JSON schema" in prompt
    assert '"rooms"' in prompt


@pytest.mark.usefixtures("init_integration")
async def test_generate_structured_data_invalid(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test an answer that isn't JSON fails the task."""
    mock_opencode.turns = [text_turn("The kitchen is warm.")]

    with pytest.raises(HomeAssistantError, match="structure the task asked for"):
        await ai_task.async_generate_data(
            hass,
            task_name="Rooms",
            entity_id=ENTITY_ID,
            instructions="Which rooms are warm?",
            structure=probatio.Schema({probatio.Required("rooms"): [str]}),
        )


@pytest.mark.usefixtures("init_integration")
async def test_generate_data_unreachable(
    hass: HomeAssistant, mock_opencode: FakeOpenCode
) -> None:
    """Test a server that can't start a session fails the task."""
    mock_opencode.async_create_session.side_effect = OpenCodeConnectionError

    with pytest.raises(HomeAssistantError, match="reach the OpenCode server"):
        await ai_task.async_generate_data(
            hass,
            task_name="Weather",
            entity_id=ENTITY_ID,
            instructions="What's the weather like?",
        )
    mock_opencode.async_archive.assert_not_awaited()
