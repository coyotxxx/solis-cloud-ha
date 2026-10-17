"""Sensor platform for Solis Cloud integration."""
import asyncio
import logging
import time
from datetime import timedelta
import aiohttp
import hashlib
import hmac
import base64
import json
from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPower,
    UnitOfEnergy,
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfTemperature,
    UnitOfFrequency,
    PERCENTAGE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN, CONF_KEY_ID, CONF_KEY_SECRET, CONF_STATION_ID, CONF_API_URL,
    UPDATE_INTERVAL, MAX_RETRIES, RETRY_BACKOFF, MIN_REQUEST_SPACING,
)

_LOGGER = logging.getLogger(__name__)

# Sensor definitions
SENSOR_TYPES = [
    # Production data
    ("current_power", "Aktualna moc", UnitOfPower.KILO_WATT, SensorDeviceClass.POWER, "mdi:solar-power"),
    ("daily_energy", "Energia dzisiaj", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, "mdi:counter"),
    ("monthly_energy", "Energia miesiąc", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, "mdi:calendar-month"),
    ("yearly_energy", "Energia rok", UnitOfEnergy.MEGA_WATT_HOUR, SensorDeviceClass.ENERGY, "mdi:calendar-year"),
    ("total_energy", "Energia łącznie", UnitOfEnergy.MEGA_WATT_HOUR, SensorDeviceClass.ENERGY, "mdi:sigma"),
    
    # Operational data
    ("daily_hours", "Godziny pracy dzisiaj", "h", None, "mdi:clock-outline"),
    ("inverter_temp", "Temperatura falownika", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, "mdi:thermometer"),
    
    # PV strings
    ("pv1_voltage", "PV1 Napięcie", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "mdi:lightning-bolt"),
    ("pv1_current", "PV1 Prąd", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "mdi:current-dc"),
    ("pv1_power", "PV1 Moc", UnitOfPower.WATT, SensorDeviceClass.POWER, "mdi:solar-panel"),
    ("pv2_voltage", "PV2 Napięcie", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "mdi:lightning-bolt"),
    ("pv2_current", "PV2 Prąd", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "mdi:current-dc"),
    ("pv2_power", "PV2 Moc", UnitOfPower.WATT, SensorDeviceClass.POWER, "mdi:solar-panel"),
    
    # AC output
    ("ac_l1_voltage", "L1 Napięcie", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "mdi:sine-wave"),
    ("ac_l1_current", "L1 Prąd", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "mdi:current-ac"),
    ("ac_l2_voltage", "L2 Napięcie", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "mdi:sine-wave"),
    ("ac_l2_current", "L2 Prąd", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "mdi:current-ac"),
    ("ac_l3_voltage", "L3 Napięcie", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "mdi:sine-wave"),
    ("ac_l3_current", "L3 Prąd", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "mdi:current-ac"),
    ("ac_frequency", "Częstotliwość", UnitOfFrequency.HERTZ, SensorDeviceClass.FREQUENCY, "mdi:sine-wave"),
    
    # Status
    ("status", "Status", None, None, "mdi:check-circle"),
    ("efficiency", "Sprawność", PERCENTAGE, None, "mdi:percent"),
]


class SolisCloudDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from Solis Cloud API."""

    def __init__(self, hass: HomeAssistant, session: aiohttp.ClientSession, config: dict) -> None:
        """Initialize."""
        self.session = session
        self.key_id = config[CONF_KEY_ID]
        self.key_secret = config[CONF_KEY_SECRET]
        self.station_id = config[CONF_STATION_ID]
        self.api_url = config.get(CONF_API_URL, "https://www.soliscloud.com:13333")
        self.inverter_id = None
        self.inverter_sn = None
        # v1.1.0: wymuszenie odstepu miedzy zapytaniami do API
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    def _create_signature(self, verb: str, resource: str, body: str, date: str) -> tuple:
        """Create API signature."""
        content_md5 = hashlib.md5(body.encode('utf-8')).hexdigest()
        string_to_sign = f"{verb}\n{content_md5}\napplication/json\n{date}\n{resource}"
        signature = base64.b64encode(
            hmac.new(
                self.key_secret.encode('utf-8'),
                string_to_sign.encode('utf-8'),
                hashlib.sha1
            ).digest()
        ).decode('utf-8')
        return signature, content_md5

    async def _make_request(self, endpoint: str, body: dict = None) -> dict:
        """Wykonaj zapytanie do API z ponawianiem i odstepem miedzy zadaniami.

        v1.1.0: wczesniej KAZDY blad (HTTP != 200, success=false, timeout)
        natychmiast rzucal UpdateFailed, co gasilo wszystkie 22 encje na caly
        cykl (2 min). Teraz pojedyncza wpadka API jest ponawiana i nie wywraca
        calej integracji.
        """
        if body is None:
            body = {}

        verb = "POST"
        resource = endpoint
        body_str = json.dumps(body)
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            # Odstep miedzy DOWOLNYMI dwoma zapytaniami - limit SolisCloud.
            # Lock, bo cykl wysyla 3 zapytania i nie moga sie wyprzedzac.
            async with self._request_lock:
                czekaj = MIN_REQUEST_SPACING - (time.monotonic() - self._last_request)
                if czekaj > 0:
                    await asyncio.sleep(czekaj)
                self._last_request = time.monotonic()

            # Sygnature i naglowek Date liczymy TUZ przed wyslaniem - przy
            # ponawianiu stary Date moglby zostac odrzucony przez API.
            date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            signature, content_md5 = self._create_signature(verb, resource, body_str, date)
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'API {self.key_id}:{signature}',
                'Date': date,
                'Content-MD5': content_md5
            }

            try:
                async with self.session.post(
                    f"{self.api_url}{resource}",
                    data=body_str,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        if result.get("success"):
                            if attempt > 1:
                                _LOGGER.debug(
                                    "Solis %s: udalo sie przy probie %s/%s",
                                    endpoint, attempt, MAX_RETRIES
                                )
                            return result
                        last_error = f"API error: {result.get('msg', 'Unknown')}"
                    else:
                        last_error = f"HTTP {response.status}"
            except Exception as e:
                last_error = f"Request failed: {e}"

            if attempt < MAX_RETRIES:
                opoznienie = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
                _LOGGER.debug(
                    "Solis %s: proba %s/%s nieudana (%s), ponawiam za %ss",
                    endpoint, attempt, MAX_RETRIES, last_error, opoznienie
                )
                await asyncio.sleep(opoznienie)

        raise UpdateFailed(f"{endpoint}: {last_error} (po {MAX_RETRIES} probach)")

    async def _fetch_data(self) -> dict:
        """Fetch data from API."""
        data = {}

        # Station detail
        station_detail = await self._make_request("/v1/api/stationDetail", {"id": self.station_id})
        if station_detail:
            station_data = station_detail.get("data", {})
            data.update({
                "current_power": station_data.get("power", 0),
                "daily_energy": station_data.get("dayEnergy", 0),
                "monthly_energy": station_data.get("monthEnergy", 0),
                "yearly_energy": station_data.get("yearEnergy", 0),
                "total_energy": station_data.get("allEnergy", 0),
                "status": "Online" if station_data.get("state", 0) == 1 else "Offline"
            })

        # Station day data
        today = datetime.now().strftime("%Y-%m-%d")
        station_day = await self._make_request("/v1/api/stationDay", {
            "id": self.station_id,
            "time": today
        })
        if station_day:
            day_data = station_day.get("data", {})
            if isinstance(day_data, dict):
                data["daily_hours"] = day_data.get("fullHour", 0)
            elif isinstance(day_data, list) and day_data:
                data["daily_hours"] = day_data[0].get("fullHour", 0)
            else:
                data["daily_hours"] = 0
        # Get inverter info if not cached
        if not self.inverter_id:
            inverter_list = await self._make_request("/v1/api/inverterList", {
                "stationId": self.station_id,
                "pageNo": 1,
                "pageSize": 10
            })
            if (inverter_list and 
                inverter_list.get("data", {}).get("page", {}).get("records")):
                inverter = inverter_list["data"]["page"]["records"][0]
                self.inverter_id = inverter["id"]
                self.inverter_sn = inverter["sn"]

        # Inverter detail
        if self.inverter_id and self.inverter_sn:
            inverter_detail = await self._make_request("/v1/api/inverterDetail", {
                "id": self.inverter_id,
                "sn": self.inverter_sn
            })
            if inverter_detail:
                inv_data = inverter_detail.get("data", {})
                data.update({
                    "inverter_temp": inv_data.get("inverterTemperature", 0),
                    "pv1_voltage": inv_data.get("uPv1", 0),
                    "pv1_current": inv_data.get("iPv1", 0),
                    "pv1_power": inv_data.get("pow1", 0),  # Zmienione z pPv1 na pow1
                    "pv2_voltage": inv_data.get("uPv2", 0),
                    "pv2_current": inv_data.get("iPv2", 0),
                    "pv2_power": inv_data.get("pow2", 0),  # Zmienione z pPv2 na pow2
                    "ac_l1_voltage": inv_data.get("uAc1", 0),
                    "ac_l1_current": inv_data.get("iAc1", 0),
                    "ac_l2_voltage": inv_data.get("uAc2", 0),
                    "ac_l2_current": inv_data.get("iAc2", 0),
                    "ac_l3_voltage": inv_data.get("uAc3", 0),
                    "ac_l3_current": inv_data.get("iAc3", 0),
                    "ac_frequency": inv_data.get("fAc", 0),
                    "efficiency": inv_data.get("efficiency", 0),
                })

        return data

    async def _async_update_data(self) -> dict:
        """Update data via library."""
        return await self._fetch_data()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    session = async_get_clientsession(hass)
    config = hass.data[DOMAIN][config_entry.entry_id]
    
    coordinator = SolisCloudDataUpdateCoordinator(hass, session, config)
    
    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()
    
    # Create sensors
    entities = []
    for sensor_type, name, unit, device_class, icon in SENSOR_TYPES:
        entities.append(
            SolisCloudSensor(coordinator, config_entry, sensor_type, name, unit, device_class, icon)
        )
    
    async_add_entities(entities)


class SolisCloudSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Solis Cloud sensor."""

    def __init__(self, coordinator, config_entry, sensor_type, name, unit, device_class, icon):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._sensor_type = sensor_type
        self._name = name
        self._unit = unit
        self._device_class = device_class
        self._icon = icon
        self._config_entry = config_entry

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"Solis {self._name}"

    @property
    def unique_id(self):
        """Return a unique ID to use for this entity."""
        return f"{self._config_entry.entry_id}_{self._sensor_type}"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.coordinator.data.get(self._sensor_type)

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def device_class(self):
        """Return the device class."""
        return self._device_class

    @property
    def icon(self):
        """Return the icon."""
        return self._icon

    @property
    def state_class(self):
        """Return the state class."""
        if self._device_class == SensorDeviceClass.ENERGY:
            return SensorStateClass.TOTAL_INCREASING
        elif self._device_class in [
            SensorDeviceClass.POWER,
            SensorDeviceClass.VOLTAGE,
            SensorDeviceClass.CURRENT,
            SensorDeviceClass.TEMPERATURE,
            SensorDeviceClass.FREQUENCY
        ]:
            return SensorStateClass.MEASUREMENT
        return None

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": "Solis Inverter",
            "manufacturer": "Solis",
            "model": "Solar Inverter",
            "sw_version": "SolisCloud API",
        }
