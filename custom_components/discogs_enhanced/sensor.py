"""Show the amount of records in a user's Discogs collection and its value,
including counts categorized by media format.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import random
import re
from typing import Any
from urllib.parse import urlparse

import discogs_client
import voluptuous as vol
import aiohttp

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import CONF_MONITORED_CONDITIONS, CONF_NAME, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import SERVER_SOFTWARE, async_get_clientsession
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

# Hardened security timeouts
_HTTP_TIMEOUT = aiohttp.ClientTimeout(connect=5, total=30)

# Strict image source CDN allowlist
_TRUSTED_IMAGE_DOMAINS = {
    "img.discogs.com",
    "i.discogs.com",
    "discogs.com",
}

SENSOR_COLLECTION_TYPE = "collection"
SENSOR_WANTLIST_TYPE = "wantlist"
SENSOR_RANDOM_RECORD_TYPE = "random_record"
SENSOR_COLLECTION_VALUE_MIN_TYPE = "collection_value_min"
SENSOR_COLLECTION_VALUE_MEDIAN_TYPE = "collection_value_median"
SENSOR_COLLECTION_VALUE_MAX_TYPE = "collection_value_max"
SENSOR_VINYL_COUNT_TYPE = "vinyl_count"
SENSOR_CD_COUNT_TYPE = "cd_count"

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


def _mask_token(token: str) -> str:
    """Return a masked token safe for logging."""
    if not token or len(token) < 4:
        return "****"
    return token[:4] + "*" * (len(token) - 4)


def _is_trusted_image_url(url: str | None) -> bool:
    """Validate that a cover_image URL belongs to a trusted Discogs domain exactly."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        hostname = parsed.hostname or ""
        hostname = hostname.lower()
        return hostname in _TRUSTED_IMAGE_DOMAINS or any(
            hostname.endswith("." + domain) for domain in _TRUSTED_IMAGE_DOMAINS
        )
    except Exception:
        return False


def _fetch_identity_and_counts(discogs_client_instance) -> dict:
    """Isolate blocking synchronous client API handshakes into an executor wrapper."""
    identity = discogs_client_instance.identity()
    return {
        "username": identity.username,
        "display_name": identity.name,
        "num_collection": identity.num_collection,
        "num_wantlist": identity.num_wantlist,
        "curr_abbr": (
            getattr(identity, "curr_abbr", None)
            or (identity.data.get("curr_abbr") if isinstance(getattr(identity, "data", None), dict) else None)
        ),
    }


class DiscogsDataStore:
    """Centralized, safe async state manager for legacy platforms."""

    def __init__(self, hass: HomeAssistant, token: str) -> None:
        """Initialize state parameters."""
        self.hass = hass
        self.token = token
        self.session = async_get_clientsession(hass)
        self._discogs_client = discogs_client.Client(SERVER_SOFTWARE, user_token=token)
        
        self.data: dict[str, Any] = {
            "user": "Unknown",
            "cached_releases": [],
            "collection_count": 0,
            "wantlist_count": 0,
            "collection_value_min": "0.00",
            "collection_value_median": "0.00",
            "collection_value_max": "0.00",
            "currency_symbol": "$",
            SENSOR_VINYL_COUNT_TYPE: 0,
            SENSOR_CD_COUNT_TYPE: 0,
        }

    async def async_update_data(self) -> None:
        """Fetch remote datasets securely and compute format parsing without making blocking calls."""
        _LOGGER.debug("Requesting absolute update sequence for Discogs metrics.")
        
        # 1. Identity Verification via Thread Executor (Only fetch text variables)
        try:
            identity_data = await self.hass.async_add_executor_job(
                _fetch_identity_and_counts, self._discogs_client
            )
        except Exception as err:
            _LOGGER.error("Authentication or connection block failure on Discogs backend: %s", err)
            return

        username = identity_data["username"]
        currency_symbol = identity_data["curr_abbr"] or "$"

        self.data.update({
            "user": identity_data["display_name"] or username,
            "collection_count": identity_data["num_collection"],
            "wantlist_count": identity_data["num_wantlist"],
            "currency_symbol": currency_symbol,
        })

        headers = {
            "User-Agent": SERVER_SOFTWARE,
            "Authorization": f"Discogs token={self.token}",
        }

        # 2. Async Collection Valuation Evaluation
        value_url = f"https://api.discogs.com/users/{username}/collection/value"
        try:
            async with self.session.get(value_url, headers=headers, timeout=_HTTP_TIMEOUT, ssl=True) as resp:
                if resp.status == 200:
                    collection_value_raw = await resp.json(content_type=None)
                    if collection_value_raw and isinstance(collection_value_raw, dict):
                        self.data["collection_value_min"] = collection_value_raw.get("minimum", "0.00")
                        self.data["collection_value_median"] = collection_value_raw.get("median", "0.00")
                        self.data["collection_value_max"] = collection_value_raw.get("maximum", "0.00")
        except Exception as err:
            _LOGGER.debug("Could not parse valuation payload asynchronously: %s", err)

        # 3. High-Performance Asynchronous Collection Releases Fetching (Avoids client library loops!)
        releases_url = f"https://api.discogs.com/users/{username}/collection/folders/0/releases?per_page=100&sort=added&sort_order=desc"
        try:
            async with self.session.get(releases_url, headers=headers, timeout=_HTTP_TIMEOUT, ssl=True) as resp:
                if resp.status == 200:
                    releases_payload = await resp.json(content_type=None)
                    raw_releases = releases_payload.get("releases", [])
                    self.data["cached_releases"] = raw_releases

                    # Securely parse format types purely in memory using the non-blocking response window
                    vinyl_count = 0
                    cd_count = 0
                    for item in raw_releases:
                        basic_info = item.get("basic_information", {})
                        formats = basic_info.get("formats", [])
                        if formats and formats[0].get("name"):
                            primary_format = formats[0]["name"].lower()
                            if "vinyl" in primary_format:
                                vinyl_count += 1
                            elif "cd" in primary_format:
                                cd_count += 1
                    
                    self.data[SENSOR_VINYL_COUNT_TYPE] = vinyl_count
                    self.data[SENSOR_CD_COUNT_TYPE] = cd_count
        except Exception as err:
            _LOGGER.error("Failed to parse collection releases payload asynchronously: %s", err)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the legacy platform layout securely while ensuring entities map cleanly."""
    token = config[CONF_TOKEN]
    name = config[CONF_NAME]

    store = DiscogsDataStore(hass, token)
    await store.async_update_data()

    monitored_conditions = config[CONF_MONITORED_CONDITIONS]
    
    legacy_id_translations = {
        SENSOR_COLLECTION_TYPE: "sensor.discogs_collection",
        SENSOR_WANTLIST_TYPE: "sensor.discogs_wantlist",
        SENSOR_RANDOM_RECORD_TYPE: "sensor.discogs_random_record",
        SENSOR_COLLECTION_VALUE_MIN_TYPE: "sensor.discogs_collection_value_min",
        SENSOR_COLLECTION_VALUE_MEDIAN_TYPE: "sensor.discogs_collection_value_median",
        SENSOR_COLLECTION_VALUE_MAX_TYPE: "sensor.discogs_collection_value_max",
        SENSOR_VINYL_COUNT_TYPE: "sensor.discogs_vinyl_records",
        SENSOR_CD_COUNT_TYPE: "sensor.discogs_cds",
    }

    entities = []
    for description in SENSOR_TYPES:
        if description.key in monitored_conditions:
            target_id = legacy_id_translations.get(description.key, f"sensor.discogs_{description.key}")
            entities.append(DiscogsSensor(store, name, description, target_id))

    async_add_entities(entities, True)


class DiscogsSensor(SensorEntity):
    """Secure, high-performance representation of a Discogs sensor entry."""

    _attr_attribution = "Data provided by Discogs"

    def __init__(
        self, store: DiscogsDataStore, name: str, description: SensorEntityDescription, target_id: str
    ) -> None:
        """Initialize parameters and clamp historical entity registry mapping boundaries."""
        self.entity_description = description
        self._store = store
        self._attrs: dict = {}
        self._attr_name = f"{name} {description.name}"
        self.entity_id = target_id

    @property
    def device_class(self) -> str | None:
        """Return target configuration class rules."""
        if self.entity_description.key in [
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ]:
            return "monetary"
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Dynamically expose the target local currency symbol cleanly."""
        if self.entity_description.key in [
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ]:
            return self._store.data.get("currency_symbol", "$")
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose operational variables safely without breaking core pipelines."""
        if self.native_value is None:
            return None

        attributes: dict = {ATTR_IDENTITY: self._store.data["user"]}

        if self.entity_description.key == SENSOR_RANDOM_RECORD_TYPE and self._attrs:
            basic_info = self._attrs if "basic_information" not in self._attrs else self._attrs.get("basic_information", {})
            formats = basic_info.get("formats", [{}])
            first_format = formats[0] if formats else {}
            format_name = first_format.get("name")
            descriptions = first_format.get("descriptions", [])

            format_str = None
            if format_name:
                format_parts = [format_name]
                if descriptions:
                    format_parts.append(f"({', '.join(descriptions)})")
                format_str = " ".join(format_parts)

            labels = basic_info.get("labels", [{}])
            first_label = labels[0] if labels else {}

            raw_cover_url = basic_info.get("cover_image")
            safe_cover_url = raw_cover_url if _is_trusted_image_url(raw_cover_url) else None

            attributes.update(
                {
                    "cat_no": first_label.get("catno"),
                    "cover_image": safe_cover_url,
                    "format": format_str,
                    "label": first_label.get("name"),
                    "released": basic_info.get("year"),
                }
            )
        return attributes

    def _get_random_record_from_cache(self) -> str | None:
        """Instantly pick a random release item from the cleanly pre-fetched cache array."""
        releases = self._store.data.get("cached_releases", [])
        if releases:
            chosen_item = random.choice(releases)
            basic_info = chosen_item.get("basic_information", {})
            self._attrs = basic_info
            
            artists = basic_info.get("artists", [{}])
            artist_name = artists[0].get("name", "Unknown Artist") if artists else "Unknown Artist"
            title = basic_info.get("title", "Unknown Title")
            return f"{artist_name} - {title}"
        return "No items found"

    async def async_update(self) -> None:
        """Coordinate localized updates tracking original functional signatures exactly."""
        _LOGGER.debug("Polling track variables for entity: %s", self.entity_description.key)
        
        await self._store.async_update_data()
        
        key = self.entity_description.key
        discogs_data = self._store.data

        if key == SENSOR_COLLECTION_TYPE:
            self._attr_native_value = discogs_data["collection_count"]
        elif key == SENSOR_WANTLIST_TYPE:
            self._attr_native_value = discogs_data["wantlist_count"]
        elif key == SENSOR_VINYL_COUNT_TYPE:
            self._attr_native_value = discogs_data.get(SENSOR_VINYL_COUNT_TYPE, 0)
        elif key == SENSOR_CD_COUNT_TYPE:
            self._attr_native_value = discogs_data.get(SENSOR_CD_COUNT_TYPE, 0)
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
            value_str = discogs_data.get(data_key)

            if isinstance(value_str, str) and value_str:
                numeric_value_str = re.sub(r"[^\d.]", "", value_str.replace(",", ""))
                if numeric_value_str:
                    try:
                        self._attr_native_value = float(numeric_value_str)
                    except ValueError:
                        self._attr_native_value = None
                else:
                    self._attr_native_value = None
            else:
                self._attr_native_value = None
        else:
            self._attr_native_value = self._get_random_record_from_cache()
