"""The OpenCode integration."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import OpenCodeAuthError, OpenCodeClient, OpenCodeError
from .const import (
    CONF_RENEWED_AT,
    DOMAIN,
    LOGGER,
    TOKEN_CHECK_INTERVAL,
    TOKEN_RENEW_AFTER,
)

PLATFORMS = (Platform.AI_TASK, Platform.CONVERSATION)


@dataclass(slots=True)
class OpenCodeData:
    """The client, and what the entry was set up with."""

    client: OpenCodeClient
    version: str
    # Compared by the update listener, so a renewed token doesn't reload
    data: dict[str, Any]
    subentries: dict[str, ConfigSubentry]


type OpenCodeConfigEntry = ConfigEntry[OpenCodeData]


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
        subentries=dict(entry.subentries),
    )
    await async_renew_token(hass, entry)

    async def async_check_token(_: datetime) -> None:
        await async_renew_token(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(hass, async_check_token, TOKEN_CHECK_INTERVAL)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> bool:
    """Unload OpenCode."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_options(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> None:
    """Reload the entry when it changes, but not when its token renews."""
    if (
        dict(entry.data) != entry.runtime_data.data
        or dict(entry.subentries) != entry.runtime_data.subentries
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
