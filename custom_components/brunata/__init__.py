"""The Brunata integration."""
from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timedelta

try:
    # httpx is a dependency of brunata_api and always available at runtime.
    # The IDE may not resolve it if httpx is not installed in the dev environment.
    import httpx as _httpx
    _CONNECT_ERRORS = (ConnectionError, UnboundLocalError, _httpx.ConnectError, _httpx.ConnectTimeout, _httpx.ReadTimeout)
except ImportError:
    _CONNECT_ERRORS = (ConnectionError, UnboundLocalError)

from brunata_api import Client
from brunata_api.const import OAUTH2_URL, CLIENT_ID

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_DEBUG_LOGGING

_LOGGER = logging.getLogger(__name__)


async def _check_connectivity(host: str, port: int = 443, timeout: float = 5.0) -> bool:
    """Quick TCP check to verify network reachability before invoking the library."""
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, socket.create_connection, (host, port)),
            timeout=timeout,
        )
        return True
    except Exception:
        return False


# Monkeypatch to fix bug in brunata_api library where it awaits a dict in _renew_tokens
async def _renew_tokens_fixed(self) -> dict:
    """Renew access token using refresh token."""
    if self._is_token_valid("access_token"):
        _LOGGER.debug(
            "Token is not expired, expires in %d seconds",
            self._tokens.get("expires_on") - int(datetime.now().timestamp()),
        )
        return self._tokens
    # Get OAuth 2.0 token object
    try:
        tokens = await self.api_wrapper(
            method="POST",
            url=f"{OAUTH2_URL}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._tokens.get("refresh_token"),
                "CLIENT_ID": CLIENT_ID,
            },
        )
    except UnboundLocalError:
        # Library bug: api_wrapper catches ConnectError but then tries to return an
        # unassigned 'response' variable — treat this as a network connectivity error.
        raise ConnectionError("Brunata token server unreachable") from None
    except Exception:
        _LOGGER.exception("An error occurred while trying to renew tokens")
        raise
    return tokens.json()

Client._renew_tokens = _renew_tokens_fixed

PLATFORMS: list[str] = ["sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Brunata from a config entry."""
    if entry.options.get(CONF_DEBUG_LOGGING):
        _LOGGER.setLevel(logging.DEBUG)
        logging.getLogger("brunata_api").setLevel(logging.DEBUG)
        _LOGGER.debug("Debug logging enabled via settings")

    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]

    if not await _check_connectivity("online.brunata.com"):
        raise ConfigEntryNotReady("Cannot reach Brunata servers — network not ready, will retry")

    _LOGGER.debug("Setting up Brunata integration for %s", email)
    client = await hass.async_add_executor_job(Client, email, password)
    coordinator = BrunataDataUpdateCoordinator(hass, client)

    # Initial data refresh
    _LOGGER.debug("Performing initial data refresh")
    try:
        await coordinator.async_config_entry_first_refresh()
    except UpdateFailed as err:
        if "authentication failed" in str(err).lower():
            raise ConfigEntryAuthFailed from err
        raise

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    _LOGGER.debug("Forwarding setups to platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

class BrunataDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Brunata data."""

    def __init__(self, hass: HomeAssistant, client: Client):
        """Initialize."""
        self.client = client
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=1),
        )

    async def _async_update_data(self):
        """Fetch data from API."""
        _LOGGER.debug("Starting data update from Brunata API")
        try:
            # We fetch meters without startdate to get the absolute latest measurements
            # for all meters. Brunata's API returns the first measurement in a period
            # if startdate is specified, which gives outdated data.
            
            # The library has a bug where it tries to await a dict
            # in _get_tokens -> _renew_tokens and _b2c_auth.
            # We wrap it in a try-except to provide a better error message
            # if it fails in the library.
            try:
                _LOGGER.debug("Refreshing tokens and initializing mappers")
                await self.client._get_tokens()
                await self.client._init_mappers()
            except TypeError as err:
                if "await" in str(err) and "dict" in str(err):
                    _LOGGER.error("Error in brunata-api library: 'object dict can't be used in await expression'. Ensure you have a fixed version of the library or contact the developer.")
                raise UpdateFailed(f"Error communicating with Brunata API via library: {err}") from err

            from brunata_api.const import API_URL, METERS_URL
            from brunata_api import Meter

            # Fetch all meters with their latest status
            _LOGGER.debug("Fetching meters from %s/consumer/meters", API_URL)
            response = await self.client.api_wrapper(
                method="GET",
                url=f"{API_URL}/consumer/meters",
                headers={
                    "Referer": METERS_URL,
                },
            )

            if response is None:
                _LOGGER.warning("No response from API (timeout or connection error)")
                return dict(self.client._meters)

            _LOGGER.debug("API response from /consumer/meters: %s", response.text)

            try:
                result = response.json()
            except Exception as json_err:
                _LOGGER.error("Error parsing JSON from API: %s. Response: %s", json_err, response.text)
                return dict(self.client._meters)

            if not isinstance(result, list):
                if isinstance(result, dict) and (
                    result.get("errorCode") is not None
                    or result.get("errorMessage") is not None
                ):
                    error_code = result.get("errorCode") or result.get("error_code")
                    error_message = result.get("errorMessage") or result.get("error_message")
                    _LOGGER.error(
                        "Brunata API returned error response: %s %s",
                        error_code,
                        error_message,
                    )
                    if error_code == "WB_WEBSERVICES_0011" or (
                        isinstance(error_message, str)
                        and "Not authorized" in error_message
                    ):
                        raise ConfigEntryAuthFailed(
                            "Brunata API authentication failed. Check credentials and account access."
                        )
                    raise UpdateFailed(
                        f"Brunata API returned error {error_code}: {error_message}"
                    )

                _LOGGER.error("Unexpected API response format: expected list, got %s. Response: %s", type(result), response.text)
                return dict(self.client._meters)

            # Clear existing meters so readings don't accumulate across updates
            self.client._meters.clear()

            _LOGGER.debug("Processing %s items from API", len(result))
            for item in result:
                if not isinstance(item, dict):
                    continue

                json_meter = item.get("meter")
                if not isinstance(json_meter, dict):
                    continue

                # Filter meters without superAllocationUnit (often inactive or internal devices)
                if json_meter.get("superAllocationUnit") is None:
                    _LOGGER.debug("Skipping meter %s as it has no superAllocationUnit", json_meter.get("meterId"))
                    continue

                json_reading = item.get("reading")
                meter_id = str(json_meter.get("meterId"))

                _LOGGER.debug("Processing meter %s: %s", meter_id, json_meter.get("meterNo"))

                meter = Meter(self.client, json_meter)
                self.client._meters[meter_id] = meter

                if isinstance(json_reading, dict) and json_reading.get("value") is not None:
                    _LOGGER.debug(
                        "Adding reading for %s: %s (date: %s). Raw data: %s",
                        meter_id,
                        json_reading.get("value"),
                        json_reading.get("readingDate"),
                        json_reading,
                    )
                    meter.add_reading(json_reading)

            if not self.client._meters:
                _LOGGER.warning("No meters found. Attempting default fetch via get_meters().")
                try:
                    meters = await self.client.get_meters()
                    if meters:
                        _LOGGER.debug("Found %s meters via get_meters()", len(meters))
                        # get_meters() populates self.client._meters; return a copy of it
                        return dict(self.client._meters)
                except Exception as get_meters_err:
                    _LOGGER.error("Error calling get_meters(): %s", get_meters_err)

            # Return a copy of the dictionary to ensure the coordinator detects changes
            _LOGGER.debug("Data update complete. Total meters: %s", len(self.client._meters))
            return dict(self.client._meters)
        except UpdateFailed:
            raise
        except _CONNECT_ERRORS as err:
            # Covers httpx.ConnectError/ConnectTimeout/ReadTimeout (network unavailable),
            # ConnectionError (raised by _renew_tokens_fixed), and UnboundLocalError
            # (library bug in api_wrapper when a ConnectError occurs mid-call).
            if self.data is not None:
                # Keep sensors available with their last known values instead of going unavailable.
                _LOGGER.info("Cannot connect to Brunata — keeping last known values")
                return self.data
            raise UpdateFailed("Cannot connect to Brunata — will retry next interval") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching data: {err}") from err
