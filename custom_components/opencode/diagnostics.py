"""Diagnostics for the OpenCode integration."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from . import OpenCodeConfigEntry

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OpenCodeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "version": entry.runtime_data.version,
        "subentries": {
            subentry.subentry_id: {
                "title": subentry.title,
                "subentry_type": subentry.subentry_type,
                "data": dict(subentry.data),
            }
            for subentry in entry.subentries.values()
        },
    }
