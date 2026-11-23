"""The OpenCode integration."""

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OpenCodeAuthError, OpenCodeClient, OpenCodeError
from .const import DOMAIN

PLATFORMS = (Platform.CONVERSATION,)


@dataclass(slots=True)
class OpenCodeData:
    """The client and server details for a config entry."""

    client: OpenCodeClient
    version: str


type OpenCodeConfigEntry = ConfigEntry[OpenCodeData]


async def async_setup_entry(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> bool:
    """Set up OpenCode from a config entry."""
    client = OpenCodeClient(
        async_get_clientsession(hass),
        entry.data[CONF_URL],
        entry.data.get(CONF_PASSWORD),
    )
    try:
        # The info endpoint is open, so check the password with an agent list
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

    entry.runtime_data = OpenCodeData(client=client, version=info["version"])
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> bool:
    """Unload OpenCode."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_options(hass: HomeAssistant, entry: OpenCodeConfigEntry) -> None:
    """Reload the entry when its subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)
