"""The OpenCode integration."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LLM_HASS_API, CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import OpenCodeAuthError, OpenCodeClient, OpenCodeError
from .const import (
    CONF_DIRECTORY,
    CONF_RENEWED_AT,
    DOMAIN,
    LOGGER,
    TOKEN_CHECK_INTERVAL,
    TOKEN_RENEW_AFTER,
)
from .mcp import HomeAssistantTools

PLATFORMS = (Platform.AI_TASK, Platform.CONVERSATION)


@dataclass(slots=True)
class OpenCodeData:
    """The client, and what the entry was set up with."""

    client: OpenCodeClient
    version: str
    # Compared by the update listener, so a renewed token doesn't reload
    data: dict[str, Any]
    subentries: dict[str, tuple[str, dict[str, Any]]]
    # Each session that can use Home Assistant's tools, with its latest turn's API
    tool_sessions: dict[str, llm.APIInstance] = field(default_factory=dict)


type OpenCodeConfigEntry = ConfigEntry[OpenCodeData]


def _tool_apis(entry: OpenCodeConfigEntry) -> dict[str | None, list[str]]:
    """Return the APIs the agents in each directory use, to serve there."""
    directories: dict[str | None, list[str]] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue
        apis = directories.setdefault(subentry.data.get(CONF_DIRECTORY) or None, [])
        apis.extend(
            api for api in subentry.data.get(CONF_LLM_HASS_API) or [] if api not in apis
        )
    return {directory: apis for directory, apis in directories.items() if apis}


def _subentries(entry: OpenCodeConfigEntry) -> dict[str, tuple[str, dict[str, Any]]]:
    """Copy each subentry's title and data, since Core updates them in place."""
    return {
        subentry_id: (subentry.title, dict(subentry.data))
        for subentry_id, subentry in entry.subentries.items()
    }


async def async_setup_entry(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> bool:
    """Set up OpenCode from a config entry."""
    client = OpenCodeClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN]
    )
    try:
        # The info endpoint is open, so check the token with an agent list
        info = await client.async_get_info()
        await client.async_get_agents()
    except OpenCodeAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except OpenCodeError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err

    entry.runtime_data = OpenCodeData(
        client=client,
        version=info["version"],
        data=dict(entry.data),
        subentries=_subentries(entry),
    )
    await async_renew_token(hass, entry)

    async def async_check_token(_: datetime) -> None:
        await async_renew_token(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(hass, async_check_token, TOKEN_CHECK_INTERVAL)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for directory, api_ids in _tool_apis(entry).items():
        tools = HomeAssistantTools(
            hass, client, directory, api_ids, entry.runtime_data.tool_sessions
        )
        entry.async_create_background_task(
            hass, tools.async_run(), f"{DOMAIN} tools in {directory or 'default'}"
        )
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> bool:
    """Unload OpenCode."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_options(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> None:
    """Reload the entry when it changes, but not when its token renews."""
    if (
        dict(entry.data) != entry.runtime_data.data
        or _subentries(entry) != entry.runtime_data.subentries
    ):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_renew_token(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> None:
    """Swap the paired token for a new one once it's old enough."""
    now = dt_util.utcnow().timestamp()
    if now - entry.data.get(CONF_RENEWED_AT, 0) < TOKEN_RENEW_AFTER.total_seconds():
        return
    try:
        token = await entry.runtime_data.client.async_renew_token()
    except OpenCodeAuthError:
        entry.async_start_reauth(hass)
        return
    except OpenCodeError as err:
        LOGGER.warning("Couldn't renew the OpenCode token, trying again later: %s", err)
        return
    entry.runtime_data.data = {**entry.data, CONF_TOKEN: token, CONF_RENEWED_AT: now}
    hass.config_entries.async_update_entry(entry, data=entry.runtime_data.data)
