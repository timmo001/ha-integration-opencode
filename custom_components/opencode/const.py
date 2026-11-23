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
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
    "permission.asked",
    "form.created",
)

DEFAULT_CONVERSATION_NAME = "OpenCode conversation"
DEFAULT_AGENT = "build"
DEFAULT_TIMEOUT = 60
MIN_TIMEOUT = 5
MAX_TIMEOUT = 600

REQUEST_TIMEOUT = 10

PERMISSION_REJECT_MESSAGE = (
    "This question came from a Home Assistant voice assistant, "
    "where nobody can approve this. Answer without it."
)
