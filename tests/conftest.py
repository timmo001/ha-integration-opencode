"""Fixtures for the OpenCode tests."""

from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_PROMPT, CONF_TIMEOUT, CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opencode.api import OpenCodeEvent
from custom_components.opencode.const import CONF_AGENT, CONF_RENEWED_AT, DOMAIN

URL = "http://opencode.local:49374"
LINK = f"{URL}/auth/connect/abc123"

AGENTS = [
    {"id": "build", "name": "build", "mode": "primary", "hidden": False},
    {"id": "plan", "name": "plan", "mode": "primary", "hidden": False},
    {"id": "explore", "name": "explore", "mode": "subagent", "hidden": False},
]
MODELS = [
    {
        "id": "claude-haiku-5.5",
        "providerID": "github-copilot",
        "name": "Claude Haiku 5.5",
        "enabled": True,
        "capabilities": {"tools": True},
        "cost": [{"input": 0.1, "output": 0.5}],
        "time": {"released": 1791331200000},
        "variants": [{"id": "high"}, {"id": "max"}],
    },
    {
        "id": "mai-code-2-flash",
        "providerID": "github-copilot",
        "name": "MAI Code 2 Flash",
        "enabled": True,
        "capabilities": {"tools": True},
        "cost": [{"input": 0.1, "output": 0.5}],
        "time": {"released": 1795392000000},
    },
    {
        "id": "gemini-4-neon",
        "providerID": "github-copilot",
        "name": "Gemini 4 Neon",
        "enabled": True,
        "capabilities": {"tools": True},
        "cost": [{"input": 3, "output": 15}],
        "time": {"released": 1794960000000},
        "variants": [
            {"id": "xhigh"},
            {"id": "low"},
            {"id": "custom"},
            {"id": "medium"},
            {"id": "high"},
        ],
    },
    {
        "id": "hy4-flash-free",
        "providerID": "opencode",
        "name": "HY4 Flash Free",
        "enabled": True,
        "capabilities": {"tools": True},
        "cost": [{"input": 0, "output": 0}],
    },
]
SMALL_MODEL = "github-copilot/mai-code-2-flash"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture(autouse=True)
async def setup_homeassistant(hass: HomeAssistant) -> None:
    """Set up the core integration conversation needs for exposed entities."""
    assert await async_setup_component(hass, "homeassistant", {})


class FakeOpenCode:
    """An OpenCode server that answers prompts with scripted events."""

    def __init__(self) -> None:
        """Initialize the fake."""
        self.turns: list[list[OpenCodeEvent]] = []
        self.sessions: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.last_text = ""
        self.async_get_info = AsyncMock(return_value={"version": "2.4.0"})
        self.async_redeem = AsyncMock(return_value="paired-token")
        self.async_renew_token = AsyncMock(return_value="renewed-token")
        self.set_token = MagicMock()
        self.async_get_agents = AsyncMock(return_value=AGENTS)
        self.async_get_models = AsyncMock(return_value=MODELS)
        self.async_create_session = AsyncMock(side_effect=self._create_session)
        self.async_prompt = AsyncMock(side_effect=self._prompt)
        self.async_reject_permission = AsyncMock()
        self.async_cancel_form = AsyncMock()
        self.async_interrupt = AsyncMock()
        self.async_archive = AsyncMock()
        self.async_wait = AsyncMock()
        self.async_get_last_text = AsyncMock(side_effect=lambda _: self.last_text)
        self._pending: list[OpenCodeEvent] = []

    async def _create_session(self, **kwargs: Any) -> str:
        session_id = f"ses_{len(self.sessions) + 1}"
        self.sessions.append(session_id)
        return session_id

    async def _prompt(self, session_id: str, text: str) -> None:
        self.prompts.append((session_id, text))
        self._pending = [
            OpenCodeEvent(event.type, {"sessionID": session_id, **event.data})
            if event.type != "form.created"
            else event
            for event in self.turns.pop(0)
        ]

    @asynccontextmanager
    async def async_events(
        self, types: list[str]
    ) -> AsyncIterator[AsyncIterator[OpenCodeEvent]]:
        """Yield the events of the turn prompted after opening."""

        async def events() -> AsyncIterator[OpenCodeEvent]:
            # Another session's event, which the entity has to skip
            yield OpenCodeEvent("session.text.delta", {"sessionID": "ses_other"})
            while self._pending:
                yield self._pending.pop(0)

        yield events()


def text_turn(*deltas: str, message_id: str = "msg_1") -> list[OpenCodeEvent]:
    """Return the events of a turn that answers with text."""
    return [
        *(
            OpenCodeEvent(
                "session.text.delta",
                {"assistantMessageID": message_id, "ordinal": 0, "delta": delta},
            )
            for delta in deltas
        ),
        OpenCodeEvent("session.execution.succeeded", {}),
    ]


@pytest.fixture
def mock_opencode() -> Generator[FakeOpenCode]:
    """Replace the OpenCode client."""
    fake = FakeOpenCode()
    with (
        patch("custom_components.opencode.OpenCodeClient", return_value=fake),
        patch(
            "custom_components.opencode.config_flow.OpenCodeClient", return_value=fake
        ),
    ):
        yield fake


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return an OpenCode config entry with one agent."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=URL,
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
                    CONF_PROMPT: llm.DEFAULT_INSTRUCTIONS_PROMPT,
                    CONF_TIMEOUT: 10,
                },
                "title": "OpenCode conversation",
                "unique_id": None,
            }
        ],
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opencode: FakeOpenCode,
) -> MockConfigEntry:
    """Set up the integration."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
