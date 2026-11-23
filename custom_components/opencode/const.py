"""Constants for the OpenCode integration."""

from datetime import timedelta
import logging

DOMAIN = "opencode"
LOGGER = logging.getLogger(__package__)

CONF_AGENT = "agent"
CONF_DIRECTORY = "directory"
CONF_LINK = "link"
CONF_RENEWED_AT = "renewed_at"
CONF_VARIANT = "variant"

# Paired tokens last 30 days, so renew well before, and check often enough
# that a restart or a missed check doesn't matter.
TOKEN_RENEW_AFTER = timedelta(days=7)
TOKEN_CHECK_INTERVAL = timedelta(hours=12)

EVENT_TYPES = (
    "session.text.delta",
    "session.reasoning.delta",
    "session.tool.input.started",
    "session.tool.called",
    "session.tool.success",
    "session.tool.failed",
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
    "permission.asked",
    "form.created",
)

DEFAULT_AGENT = "build"
DEFAULT_TIMEOUT = 60
DEFAULT_AI_TASK_TIMEOUT = 600
MIN_TIMEOUT = 5
MAX_TIMEOUT = 3600

REQUEST_TIMEOUT = 10

# How long a turn handed off to answer later on a satellite can keep going
HANDOFF_TIMEOUT = 600
HANDOFF_MESSAGE = "On it, I'll tell you when it's done."
HANDOFF_FAILED_MESSAGE = "Sorry, OpenCode couldn't finish that."

# Sent with the first message of a conversation on a voice satellite
SPEECH_PROMPT = (
    "You're being heard through a voice assistant speaker. Answer in a few"
    " short, plain spoken sentences, with no Markdown, lists, links or code."
    " If the answer needs more, such as a list, a table, code or a document,"
    " give the short spoken answer first, then a line with only [full answer],"
    " then the full answer in Markdown. Home Assistant saves everything after"
    " that line as a notification, and doesn't read it aloud."
)
FULL_ANSWER_MESSAGE = "I've put the full answer in your notifications."

PERMISSION_REJECT_MESSAGE = (
    "This question came from a Home Assistant voice assistant, "
    "where nobody can approve this. Answer without it."
)
