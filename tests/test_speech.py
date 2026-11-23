"""Tests for cleaning answers for a speaker."""

import pytest

from custom_components.opencode.speech import full_answer, to_speech


@pytest.mark.parametrize(
    ("text", "speech", "has_more"),
    [
        ("Edinburgh.", "Edinburgh.", False),
        (
            "# Tides\nThe *Moon* pulls the __sea__.",
            "Tides.\nThe Moon pulls the sea.",
            False,
        ),
        ("- Mercury\n- Venus!\n2. Earth", "Mercury.\nVenus!\nEarth.", False),
        ("> Run `ls` in my_folder", "Run ls in my_folder", False),
        ("~~No~~ yes, it's 5 * 3 = 15.", "No yes, it's 5 * 3 = 15.", False),
        (
            "Read [the docs](https://x.io) or https://y.io now.",
            "Read the docs or the link now.",
            True,
        ),
        ("Here:\n```py\nprint(1)\n```\nDone.", "Here:\nDone.", True),
        (
            "Sizes:\n| a | b |\n|---|---|\n\n---\nThat's all.",
            "Sizes:\nThat's all.",
            True,
        ),
        ("Short answer.\n**[Full answer]**\nLong answer.", "Short answer.", True),
    ],
)
def test_to_speech(text: str, speech: str, has_more: bool) -> None:
    """Test Markdown is cleaned into plain speech."""
    assert to_speech(text) == (speech, has_more)


def test_full_answer_drops_marker() -> None:
    """Test the saved answer keeps everything but the marker."""
    assert (
        full_answer("Short.\n[full answer]\n# Long\nMore.") == "Short.\n# Long\nMore."
    )
