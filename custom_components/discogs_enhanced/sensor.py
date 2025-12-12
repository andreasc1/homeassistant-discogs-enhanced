"""Show the amount of records in a user's Discogs collection and its value,
including counts categorized by media format.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import random
import re
import json
import requests
from typing import Any

import discogs_client
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import CONF_MONITORED_CONDITIONS, CONF_NAME, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import SERVER_SOFTWARE
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

ATTR_IDENTITY = "identity"

DEFAULT_NAME = "Discogs"

ICON_RECORD = "mdi:album"
ICON_PLAYER = "mdi:record-player"
ICON_CASH = "mdi:cash"
UNIT_RECORDS = "records"

SCAN_INTERVAL = timedelta(minutes=10)

SENSOR_COLLECTION_TYPE = "collection"
SENSOR_WANTLIST_TYPE = "wantlist"
SENSOR_RANDOM_RECORD_TYPE = "random_record"
SENSOR_COLLECTION_VALUE_MIN_TYPE = "collection_value_min"
SENSOR_COLLECTION_VALUE_MEDIAN_TYPE = "collection_value_median"
SENSOR_COLLECTION_VALUE_MAX_TYPE = "collection_value_max"

# --- NEW FORMAT SENSORS ---
SENSOR_VINYL_COUNT_TYPE = "vinyl_count"
SENSOR_CD_COUNT_TYPE = "cd_count"
# --------------------------


SENSOR_TYPES: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key=SENSOR_COLLECTION_TYPE,
        name="Collection",
        icon=ICON_RECORD,
        native_unit_of_measurement=UNIT_RECORDS,
    ),
    SensorEntityDescription(
        key=SENSOR_WANTLIST_TYPE,
        name="Wantlist",
        icon=ICON_RECORD,
        native_unit_of_measurement=UNIT_RECORDS,
    ),
    SensorEntityDescription(
        key=SENSOR_RANDOM_RECORD_TYPE,
        name="Random Record",
        icon=ICON_PLAYER,
    ),
    # Monetary sensors - device_class will be set in DiscogsSensor below
    SensorEntityDescription(
        key=SENSOR_COLLECTION_VALUE_MIN_TYPE,
        name="Collection Value (Min)",
        icon=ICON_CASH,
    ),
    SensorEntityDescription(
        key=SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
        name="Collection Value (Median)",
        icon=ICON_CASH,
    ),
    SensorEntityDescription(
        key=SENSOR_COLLECTION_VALUE_MAX_TYPE,
        name="Collection Value (Max)",
        icon=ICON_CASH,
    ),
    # --- NEW FORMAT SENSOR DEFINITIONS ---
    SensorEntityDescription(
        key=SENSOR_VINYL_COUNT_TYPE,
        name="Vinyl Records",
        icon=ICON_RECORD,
        native_unit_of_measurement=UNIT_RECORDS,
    ),
    SensorEntityDescription(
        key=SENSOR_CD_COUNT_TYPE,
        name="CDs",
        icon=ICON_RECORD,
        native_unit_of_measurement=UNIT_RECORDS,
    ),
    # -------------------------------------
)
SENSOR_KEYS: list[str] = [desc.key for desc in SENSOR_TYPES]

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_TOKEN): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_MONITORED_CONDITIONS, default=SENSOR_KEYS): vol.All(
            cv.ensure_list, [vol.In(SENSOR_KEYS)]
        ),
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Discogs sensor."""
    token = config[CONF_TOKEN]
    name = config[CONF_NAME]

    _LOGGER.debug("Setting up Discogs Enhanced sensor platform.")

    _discogs_client = discogs_client.Client(SERVER_SOFTWARE, user_token=token)
    
    # Initialize values to safe defaults
    collection_value_min_str = "0.00"
    collection_value_median_str = "0.00"
    collection_value_max_str = "0.00"
    currency_symbol = "$" 
    
    # Initialize NEW format counts
    vinyl_count = 0
    cd_count = 0

    discogs_data: dict = {
        "user": "Unknown",
        "folders": [],
        "collection_count": 0,
        "wantlist_count": 0,
        "collection_value_min": collection_value_min_str,
        "collection_value_median": collection_value_median_str,
        "collection_value_max": collection_value_max_str,
        "currency_symbol": currency_symbol,
        # Set initial format counts
        SENSOR_VINYL_COUNT_TYPE: vinyl_count,
        SENSOR_CD_COUNT_TYPE: cd_count,
    }

    discogs_identity = None

    try:
        # 1. Fetch identity data first to get username and counts
        discogs_identity = _discogs_client.identity()
        _LOGGER.debug("Discogs identity fetched: %s (Username: %s)", discogs_identity.name, discogs_identity.username)
        
        # Populate basic identity data
        discogs_data["user"] = discogs_identity.name
        discogs_data["folders"] = discogs_identity.collection_folders
        discogs_data["collection_count"] = discogs_identity.num_collection
        discogs_data["wantlist_count"] = discogs_identity.num_wantlist
        
        # Update currency symbol dynamically
        if hasattr(discogs_identity, 'curr_abbr') and discogs_identity.curr_abbr:
            currency_symbol = discogs_identity.curr_abbr
        elif hasattr(discogs_identity, 'data') and isinstance(discogs_identity.data, dict) and 'curr_abbr' in discogs_identity.data and discogs_identity.data['curr_abbr']:
            currency_symbol = discogs_identity.data['curr_abbr']
        else:
            _LOGGER.warning("Could not retrieve currency abbreviation. Defaulting to '%s'.", currency_symbol)
        discogs_data["currency_symbol"] = currency_symbol

        # 2. Fetch Collection Value (using direct requests library for robustness)
        full_value_url = f"https://api.discogs.com/users/{discogs_identity.username}/collection/value"
        _LOGGER.debug("Attempting to fetch collection value: %s", full_value_url)
        
        collection_value_raw = None
        try:
            headers = {
                "User-Agent": SERVER_SOFTWARE, 
                "Authorization": f"Discogs token={token}"
            }
            response = requests.get(full_value_url, headers=headers)
            response.raise_for_status() # Raises exception on bad status (4xx or 5xx)
            
            collection_value_raw = response.json()
            # CRITICAL LOGGING: Check the raw API response for the value strings
            _LOGGER.info("Discogs API Raw Value Response: %s", collection_value_raw) 

            if collection_value_raw and isinstance(collection_value_raw, dict):
                # Discogs returns strings like "€1,792,790.00"
                collection_value_min_str = collection_value_raw.get('minimum', "0.00")
                collection_value_median_str = collection_value_raw.get('median', "0.00")
                collection_value_max_str = collection_value_raw.get('maximum', "0.00")
            else:
                _LOGGER.warning("Discogs API returned no valid dictionary data from /collection/value endpoint. Defaulting to 0.00.")

        except requests.exceptions.RequestException as req_err:
            _LOGGER.warning("RequestException when fetching collection value: %s. Check token permissions and network.", req_err)
        except json.JSONDecodeError as json_err:
            _LOGGER.warning("JSONDecodeError when parsing collection value response: %s. Response may not be valid JSON.", json_err)
        
        # Store the fetched (potentially default "0.00") strings for the update method
        discogs_data["collection_value_min"] = collection_value_min_str
        discogs_data["collection_value_median"] = collection_value_median_str
        discogs_data["collection_value_max"] = collection_value_max_str

    except discogs_client.exceptions.HTTPError as err:
        _LOGGER.error("API token is not valid or Discogs API error when fetching initial data: %s", err)
        return
    except Exception as err:
        _LOGGER.exception("An unexpected error occurred during Discogs sensor setup, falling back to defaults.")


    # --- 3. LOGIC: Fetch and Process Collection for Format Counts ---
    if discogs_identity and discogs_data["collection_count"] > 0:
        try:
            # We assume the first folder (index 0) is the total collection view.
            main_folder = discogs_data["folders"][0] if discogs_data["folders"] else None

            if main_folder:
                current_vinyl_count = 0
                current_cd_count = 0
                
                # Iterate through releases. discogs_client handles pagination automatically.
                for release_item in main_folder.releases:
                    release_data = release_item.release.data 
                    
                    formats = release_data.get('formats', [])
                    
                    if formats and formats[0].get('name'):
                        primary_format = formats[0]['name']
                        
                        if primary_format.lower() == "vinyl":
                            current_vinyl_count += 1
                        elif primary_format.lower() == "cd":
                            current_cd_count += 1

                _LOGGER.info("Collection format counts calculated: Vinyl=%d, CD=%d", current_vinyl_count, current_cd_count)

                # Update the main data dictionary
                discogs_data[SENSOR_VINYL_COUNT_TYPE] = current_vinyl_count
                discogs_data[SENSOR_CD_COUNT_TYPE] = current_cd_count

            else:
                _LOGGER.warning("Could not find the main collection folder to fetch release data.")

        except discogs_client.exceptions.HTTPError as err:
            _LOGGER.error("Permission issue or rate limit hit when fetching collection releases for format counting: %s", err)
        except Exception as err:
            _LOGGER.exception("An unexpected error occurred during collection format counting.")
    # -------------------------------------------------------------------


    monitored_conditions = config[CONF_MONITORED_CONDITIONS]
    entities = [
        DiscogsSensor(discogs_data, name, description)
        for description in SENSOR_TYPES
        if description.key in monitored_conditions
    ]

    add_entities(entities, True)


class DiscogsSensor(SensorEntity):
    """Create a new Discogs sensor for a specific type."""

    _attr_attribution = "Data provided by Discogs"
    
    # 1. FIX: Define device_class property to ensure monetary sensors are treated as numbers
    @property
    def device_class(self):
        """Return the device class."""
        if self.entity_description.key in [
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ]:
            return "monetary"
        return None

    def __init__(
        self, discogs_data, name, description: SensorEntityDescription
    ) -> None:
        """Initialize the Discogs sensor."""
        self.entity_description = description
        self._discogs_data = discogs_data
        self._attrs: dict = {}

        self._attr_name = f"{name} {description.name}"

        # Set unit of measurement for value sensors dynamically
        if description.key in [
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ]:
            self._attr_native_unit_of_measurement = self._discogs_data[
                "currency_symbol"
            ]

    @property
    def extra_state_attributes(self):
        """Return the device state attributes of the sensor."""
        if self._attr_native_value is None:
            return None

        attributes = {ATTR_IDENTITY: self._discogs_data["user"]}

        # Attributes for Random Record sensor
        if self.entity_description.key == SENSOR_RANDOM_RECORD_TYPE and self._attrs:
            first_format = self._attrs.get('formats', [{}])[0]
            format_name = first_format.get('name')
            descriptions = first_format.get('descriptions', []) 

            format_str = None
            if format_name:
                format_parts = [format_name]
                if descriptions:
                    format_parts.append(f"({', '.join(descriptions)})")
                format_str = " ".join(format_parts)
            
            first_label = self._attrs.get('labels', [{}])[0]
            label_name = first_label.get('name')
            cat_no = first_label.get('catno')

            attributes.update({
                "cat_no": cat_no,
                "cover_image": self._attrs.get("cover_image"),
                "format": format_str,
                "label": label_name,
                "released": self._attrs.get("year"),
            })
        return attributes

    def get_random_record(self) -> str | None:
        """Get a random record suggestion from the user's collection."""
        if self._discogs_data["folders"] and self._discogs_data["folders"][0].count > 0:
            collection = self._discogs_data["folders"][0]
            random_index = random.randrange(collection.count)
            random_record = collection.releases[random_index].release

            self._attrs = random_record.data
            _LOGGER.debug("Fetched random record data: %s", self._attrs)
            
            artist_name = self._attrs.get('artists', [{}])[0].get('name') if self._attrs.get('artists') else 'Unknown Artist'
            title = self._attrs.get('title', 'Unknown Title')

            return f"{artist_name} - {title}"
        _LOGGER.debug("No folders or empty first folder, cannot get random record.")
        return None

    def update(self) -> None:
        """Set state to the amount of records or collection value."""
        _LOGGER.debug("Updating Discogs sensor: %s", self.entity_description.key)

        key = self.entity_description.key
        
        if key == SENSOR_COLLECTION_TYPE:
            self._attr_native_value = self._discogs_data["collection_count"]
        elif key == SENSOR_WANTLIST_TYPE:
            self._attr_native_value = self._discogs_data["wantlist_count"]
        # --- NEW FORMAT COUNT LOGIC ---
        elif key == SENSOR_VINYL_COUNT_TYPE:
            self._attr_native_value = self._discogs_data.get(SENSOR_VINYL_COUNT_TYPE, 0)
        elif key == SENSOR_CD_COUNT_TYPE:
            self._attr_native_value = self._discogs_data.get(SENSOR_CD_COUNT_TYPE, 0)
        # ------------------------------
        elif key in [
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ]:
            key_map = {
                SENSOR_COLLECTION_VALUE_MIN_TYPE: "collection_value_min",
                SENSOR_COLLECTION_VALUE_MEDIAN_TYPE: "collection_value_median",
                SENSOR_COLLECTION_VALUE_MAX_TYPE: "collection_value_max",
            }
            data_key = key_map[key]
            value_str = self._discogs_data.get(data_key)

            if isinstance(value_str, str) and value_str:
                # Use the cleaning logic that was functional, which includes .replace(',', '')
                # to handle potential thousands separators before stripping non-numeric chars.
                numeric_value_str = re.sub(r'[^\d.]', '', value_str.replace(',', ''))
                
                if numeric_value_str:
                    try:
                        raw_value = float(numeric_value_str)
                        
                        # 1. Apply the original *1000.0 factor (the step that made the value non-zero)
                        inflated_value = raw_value * 1000.0
                        
                        # 2. FIX: Divide the result by 1000.0 to correct the magnitude.
                        # (e.g., 9501570.0 / 1000.0 = 9501.57)
                        self._attr_native_value = inflated_value / 1000.0
                        
                        _LOGGER.info("Final corrected native value set to: %s", self._attr_native_value)

                    except ValueError as e:
                        _LOGGER.error("Could not convert to float for %s: %s", data_key, e)
                        self._attr_native_value = None
                else:
                    self._attr_native_value = None
            else:
                self._attr_native_value = None
        else:
            self._attr_native_value = self.get_random_record()
