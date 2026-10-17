"""Config flow for Solis Cloud integration."""
import logging
import voluptuous as vol
import aiohttp
import hashlib
import hmac
import base64
import json
from datetime import datetime, timezone

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, DEFAULT_NAME, DEFAULT_API_URL, CONF_KEY_ID, CONF_KEY_SECRET, CONF_STATION_ID, CONF_API_URL

_LOGGER = logging.getLogger(__name__)


async def validate_api_connection(hass: HomeAssistant, data: dict) -> dict:
    """Validate API connection and get station info."""
    session = async_get_clientsession(hass)
    
    # Test API connection
    key_id = data[CONF_KEY_ID]
    key_secret = data[CONF_KEY_SECRET]
    api_url = data.get(CONF_API_URL, DEFAULT_API_URL)
    
    # Create signature for test request
    verb = "POST"
    resource = "/v1/api/userStationList"
    body = "{}"
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    
    content_md5 = hashlib.md5(body.encode('utf-8')).hexdigest()
    string_to_sign = f"{verb}\n{content_md5}\napplication/json\n{date}\n{resource}"
    signature = base64.b64encode(
        hmac.new(
            key_secret.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha1
        ).digest()
    ).decode('utf-8')
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'API {key_id}:{signature}',
        'Date': date,
        'Content-MD5': content_md5
    }
    
    try:
        async with session.post(
            f"{api_url}{resource}",
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            if response.status == 200:
                result = await response.json()
                if result.get("success"):
                    # Extract station information
                    stations = result.get("data", {}).get("page", {}).get("records", [])
                    if stations:
                        station = stations[0]
                        return {
                            "station_name": station.get("stationName", "Unknown"),
                            "station_id": station.get("id", ""),
                            "power": station.get("power", 0),
                            "total_energy": station.get("allEnergy", 0)
                        }
                    else:
                        raise Exception("No stations found in account")
                else:
                    raise Exception(f"API error: {result.get('msg', 'Unknown error')}")
            else:
                raise Exception(f"HTTP {response.status}")
    except Exception as e:
        _LOGGER.error(f"API validation failed: {e}")
        raise


class SolisCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Solis Cloud."""

    VERSION = 1
    
    @property
    def icon(self) -> str:
        """Return the icon."""
        return "mdi:solar-power"

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            try:
                # Validate the API connection
                info = await validate_api_connection(self.hass, user_input)
                
                # Use detected station ID if not provided
                if not user_input.get(CONF_STATION_ID):
                    user_input[CONF_STATION_ID] = info["station_id"]
                
                # Create entry
                title = f"Solis: {info['station_name']}"
                return self.async_create_entry(title=title, data=user_input)
                
            except Exception as e:
                _LOGGER.error(f"Config validation failed: {e}")
                errors["base"] = "cannot_connect"

        # Show form
        data_schema = vol.Schema({
            vol.Required(CONF_KEY_ID): str,
            vol.Required(CONF_KEY_SECRET): str,
            vol.Optional(CONF_STATION_ID): str,
            vol.Optional(CONF_API_URL, default=DEFAULT_API_URL): str,
        })

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "setup_url": "https://www.soliscloud.com/#/apiManage"
            }
        )
