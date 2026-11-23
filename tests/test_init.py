"""Tests for setting up OpenCode and renewing its token."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.opencode.api import OpenCodeAuthError, OpenCodeConnectionError
from custom_components.opencode.const import CONF_RENEWED_AT, DOMAIN

from .conftest import FakeOpenCode


async def test_setup_renews_old_token(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_opencode: FakeOpenCode,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a token a week old is swapped for a new one without reloading."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_RENEWED_AT: (dt_util.utcnow() - timedelta(days=8)).timestamp(),
        },
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_opencode.async_renew_token.assert_awaited_once()
    assert mock_config_entry.data[CONF_TOKEN] == "renewed-token"
    assert mock_config_entry.data[CONF_RENEWED_AT] == dt_util.utcnow().timestamp()
    assert mock_opencode.async_get_info.await_count == 1


async def test_renews_token_weekly(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_opencode: FakeOpenCode,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the token is left alone until it's a week old."""
    freezer.tick(timedelta(days=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    mock_opencode.async_renew_token.assert_not_awaited()

    freezer.tick(timedelta(days=1, hours=12))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    mock_opencode.async_renew_token.assert_awaited_once()
    assert init_integration.data[CONF_TOKEN] == "renewed-token"
    assert init_integration.state is ConfigEntryState.LOADED
    assert mock_opencode.async_get_info.await_count == 1


async def test_refused_renewal_starts_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_opencode: FakeOpenCode,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a token the server stopped accepting asks for a new pairing link."""
    mock_opencode.async_renew_token.side_effect = OpenCodeAuthError
    freezer.tick(timedelta(days=8))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert init_integration.data[CONF_TOKEN] == "token"


async def test_failed_renewal_tries_again(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_opencode: FakeOpenCode,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test an unreachable server keeps the token and tries at the next check."""
    mock_opencode.async_renew_token.side_effect = OpenCodeConnectionError
    freezer.tick(timedelta(days=8))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert "Couldn't renew the OpenCode token" in caplog.text
    assert init_integration.data[CONF_TOKEN] == "token"

    mock_opencode.async_renew_token.side_effect = None
    freezer.tick(timedelta(hours=12))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert init_integration.data[CONF_TOKEN] == "renewed-token"


async def test_renamed_subentry_renames_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Test a subentry Core changes in place still reloads, so its device follows."""
    subentry = next(iter(init_integration.subentries.values()))
    hass.config_entries.async_update_subentry(
        init_integration, subentry, title="MAI Code 2 Flash (opencode-server)"
    )
    await hass.async_block_till_done()

    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, subentry.subentry_id), init_integration.entry_id
    )
    assert device is not None
    assert device.name == "MAI Code 2 Flash (opencode-server)"
