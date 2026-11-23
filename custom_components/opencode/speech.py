"""Turn Markdown answers into plain text a speaker can read aloud."""

import re

# The line an agent puts between the spoken answer and the full one
_MARKER = re.compile(r"^\[?full answer\]?:?$", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE = re.compile(r"^\s*\|")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"<?https?://[^\s>]+>?")
_CODE = re.compile(r"`([^`]*)`")
_EMPHASIS = (
    re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"),
    re.compile(r"()(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])"),
    re.compile(r"()(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"),
    re.compile(r"(~~)(.+?)~~"),
)
_SPACES = re.compile(r"[ \t]{2,}")
_PUNCTUATION = (".", "!", "?", ":", ";", ",")


class SpeechCleaner:
    """Clean a streamed Markdown answer into plain text, a line at a time.

    A line is only spoken once it's complete, since Markdown can span it.
    """

    def __init__(self) -> None:
        """Initialize the cleaner."""
        # The answer as written
        self.text = ""
        # Whether anything was left out of what's spoken, such as code
        self.has_more = False
        self._buffer = ""
        self._in_code = False
        self._past_marker = False
        self._spoken = False

    def feed(self, delta: str) -> str:
        """Add streamed text and return what's ready to speak."""
        self.text += delta
        self._buffer += delta
        *lines, self._buffer = self._buffer.split("\n")
        return "".join(self._line(line) for line in lines)

    def flush(self) -> str:
        """Return what's left to speak at the end of the answer."""
        line, self._buffer = self._buffer, ""
        return self._line(line)

    def say(self, text: str) -> str:
        """Return text to speak after what's been spoken so far."""
        if not text:
            return ""
        separator = "\n" if self._spoken else ""
        self._spoken = True
        return f"{separator}{text}"

    def _line(self, line: str) -> str:
        if self._past_marker:
            return ""
        if _FENCE.match(line):
            self._in_code = not self._in_code
            self.has_more = True
            return ""
        if self._in_code:
            return ""
        if _TABLE.match(line):
            self.has_more = True
            return ""
        if _RULE.match(line):
            return ""

        # Headings and list items read as sentences of their own
        sentence = bool(_HEADING.match(line) or _ITEM.match(line))
        line = _ITEM.sub("", _QUOTE.sub("", _HEADING.sub("", line)))
        text = self._inline(line).strip()
        if _MARKER.match(text):
            self._past_marker = True
            self.has_more = True
            return ""
        if sentence and text and not text.endswith(_PUNCTUATION):
            text += "."
        return self.say(text)

    def _inline(self, text: str) -> str:
        if _IMAGE.search(text) or _LINK.search(text) or _URL.search(text):
            self.has_more = True
        text = _IMAGE.sub(r"\1", text)
        text = _LINK.sub(r"\1", text)
        text = _URL.sub("the link", text)
        text = _CODE.sub(r"\1", text)
        for emphasis in _EMPHASIS:
            text = emphasis.sub(r"\2", text)
        return _SPACES.sub(" ", text)


def to_speech(text: str) -> tuple[str, bool]:
    """Return an answer as plain speech, and whether anything was left out."""
    cleaner = SpeechCleaner()
    speech = cleaner.feed(text) + cleaner.flush()
    return speech, cleaner.has_more


def full_answer(text: str) -> str:
    """Return an answer as written, without the full answer marker."""
    return "\n".join(
        line for line in text.split("\n") if not _MARKER.match(line.strip(" *_"))
    ).strip()
