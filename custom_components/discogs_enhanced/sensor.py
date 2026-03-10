"""Show the amount of records in a user's Discogs collection and its value,
including counts categorized by media format.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import random
import re
import json
import asyncio
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

# FIX CVE-005: Connection and read timeouts to prevent indefinite hangs
_HTTP_TIMEOUT = aiohttp.ClientTimeout(connect=5, total=30)

# FIX CVE-008: Allowlist of trusted Discogs CDN domains for cover_image URLs
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
    """Return a masked token safe for logging (first 4 chars + asterisks)."""
    if not token or len(token) < 4:
        return "****"
    return token[:4] + "*" * (len(token) - 4)


def _is_trusted_image_url(url: str | None) -> bool:
    """FIX CVE-008: Validate that a cover_image URL belongs to a trusted Discogs domain."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        # Must be HTTPS and from an allowed domain
        if parsed.scheme != "https":
            return False
        hostname = parsed.hostname or ""
        return any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in _TRUSTED_IMAGE_DOMAINS
        )
    except Exception:
        return False


def _count_formats_in_folder(main_folder) -> tuple[int, int]:
    """FIX CVE-003: Iterate collection folder and count vinyl/CD releases.

    Returns (vinyl_count, cd_count). Kept as a pure synchronous helper so it
    can be safely dispatched to a thread-pool executor via
    hass.async_add_executor_job(), keeping the event loop free.
    """
    vinyl_count = 0
    cd_count = 0
    for release_item in main_folder.releases:
        release_data = release_item.release.data
        formats = release_data.get("formats", [])
        if formats and formats[0].get("name"):
            primary_format = formats[0]["name"].lower()
            if primary_format == "vinyl":
                vinyl_count += 1
            elif primary_format == "cd":
                cd_count += 1
    return vinyl_count, cd_count


def _fetch_identity_and_counts(discogs_client_instance) -> dict:
    """FIX CVE-001: Blocking discogs_client calls isolated for executor dispatch.

    All synchronous I/O (identity fetch + collection folder list) is grouped
    here so a single executor job covers them without touching the event loop.
    """
    identity = discogs_client_instance.identity()
    folders = identity.collection_folders
    return {
        "identity": identity,
        "username": identity.username,
        "display_name": identity.name,
        "num_collection": identity.num_collection,
        "num_wantlist": identity.num_wantlist,
        "curr_abbr": (
            getattr(identity, "curr_abbr", None)
            or (identity.data.get("curr_abbr") if isinstance(getattr(identity, "data", None), dict) else None)
        ),
        "folders": folders,
    }


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """FIX CVE-001: Async setup — all blocking I/O dispatched to executor."""
    token = config[CONF_TOKEN]
    name = config[CONF_NAME]

    _LOGGER.debug(
        "Setting up Discogs Enhanced sensor platform (token: %s).",
        _mask_token(token),  # FIX CVE-002: never log the raw token
    )

    # FIX CVE-002: Token is only held locally; never stored in discogs_data dict
    _discogs_client = discogs_client.Client(SERVER_SOFTWARE, user_token=token)

    discogs_data: dict = {
        "user": "Unknown",
        "folders": [],
        "collection_count": 0,
        "wantlist_count": 0,
        "collection_value_min": "0.00",
        "collection_value_median": "0.00",
        "collection_value_max": "0.00",
        "currency_symbol": "$",
        SENSOR_VINYL_COUNT_TYPE: 0,
        SENSOR_CD_COUNT_TYPE: 0,
    }

    # ── Step 1: Fetch identity (blocking → executor) ───────────────────────
    try:
        identity_data = await hass.async_add_executor_job(
            _fetch_identity_and_counts, _discogs_client
        )
    except discogs_client.exceptions.HTTPError as err:
        _LOGGER.error(
            "Discogs API authentication error during setup: %s", err
        )
        return
    except Exception:
        # FIX CVE-004: Log but do not silently continue — return False to HA
        _LOGGER.exception(
            "Unexpected error fetching Discogs identity. Platform will not load."
        )
        return

    currency_symbol = identity_data["curr_abbr"] or "$"
    if not identity_data["curr_abbr"]:
        _LOGGER.warning(
            "Could not retrieve currency abbreviation. Defaulting to '%s'.",
            currency_symbol,
        )

    discogs_data.update(
        {
            "user": identity_data["display_name"],
            "folders": identity_data["folders"],
            "collection_count": identity_data["num_collection"],
            "wantlist_count": identity_data["num_wantlist"],
            "currency_symbol": currency_symbol,
        }
    )

    # ── Step 2: Fetch collection value via async aiohttp ───────────────────
    # FIX CVE-001: Using HA's shared aiohttp session instead of blocking requests
    # FIX CVE-005: Explicit SSL verification (verify_ssl=True is the aiohttp default;
    #              stated explicitly here for clarity) and timeout applied
    username = identity_data["username"]
    value_url = f"https://api.discogs.com/users/{username}/collection/value"
    _LOGGER.debug("Fetching collection value from: %s", value_url)

    # FIX CVE-002: Token used only in the Authorization header, never stored in dict
    headers = {
        "User-Agent": SERVER_SOFTWARE,
        "Authorization": f"Discogs token={token}",
    }

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            value_url,
            headers=headers,
            timeout=_HTTP_TIMEOUT,
            ssl=True,               # FIX CVE-005: explicit TLS verification
        ) as response:
            response.raise_for_status()
            collection_value_raw = await response.json(content_type=None)

            # FIX CVE-010: Downgraded from INFO to DEBUG — response contains financial PII
            _LOGGER.debug("Discogs collection value API response received.")

            if collection_value_raw and isinstance(collection_value_raw, dict):
                discogs_data["collection_value_min"] = collection_value_raw.get("minimum", "0.00")
                discogs_data["collection_value_median"] = collection_value_raw.get("median", "0.00")
                discogs_data["collection_value_max"] = collection_value_raw.get("maximum", "0.00")
            else:
                _LOGGER.warning(
                    "Discogs /collection/value endpoint returned unexpected data. Defaulting to 0.00."
                )

    except aiohttp.ClientSSLError:
        # FIX CVE-005: Treat TLS failures as unrecoverable — do not continue silently
        _LOGGER.error(
            "TLS certificate verification failed for Discogs API. "
            "Possible MITM attack or misconfigured CA store. Platform will not load."
        )
        return
    except aiohttp.ClientResponseError as err:
        _LOGGER.warning(
            "HTTP error fetching collection value (status %s). "
            "Check token permissions. Values will default to 0.",
            err.status,
        )
    except aiohttp.ClientError as err:
        _LOGGER.warning(
            "Network error fetching collection value: %s. Values will default to 0.", err
        )
    except (json.JSONDecodeError, ValueError) as err:
        _LOGGER.warning(
            "Could not parse collection value response: %s. Values will default to 0.", err
        )

    # ── Step 3: Count vinyl/CD (blocking iteration → executor) ────────────
    # FIX CVE-003: Dispatched to executor so the event loop is never blocked,
    #              even for very large collections.
    if identity_data["folders"] and discogs_data["collection_count"] > 0:
        main_folder = identity_data["folders"][0] if identity_data["folders"] else None
        if main_folder:
            try:
                vinyl_count, cd_count = await hass.async_add_executor_job(
                    _count_formats_in_folder, main_folder
                )
                # FIX CVE-010: Counts are not PII but keep at DEBUG to avoid log noise
                _LOGGER.debug(
                    "Format counts: Vinyl=%d, CD=%d", vinyl_count, cd_count
                )
                discogs_data[SENSOR_VINYL_COUNT_TYPE] = vinyl_count
                discogs_data[SENSOR_CD_COUNT_TYPE] = cd_count
            except discogs_client.exceptions.HTTPError as err:
                _LOGGER.error(
                    "Rate limit or permission error counting collection formats: %s", err
                )
            except Exception:
                # FIX CVE-004: Log with full traceback; do not silently swallow
                _LOGGER.exception(
                    "Unexpected error counting collection formats. Counts will default to 0."
                )
        else:
            _LOGGER.warning("No collection folder found; format counts will be 0.")

    monitored_conditions = config[CONF_MONITORED_CONDITIONS]
    entities = [
        DiscogsSensor(discogs_data, name, description)
        for description in SENSOR_TYPES
        if description.key in monitored_conditions
    ]

    async_add_entities(entities, True)


class DiscogsSensor(SensorEntity):
    """Create a new Discogs sensor for a specific type."""

    _attr_attribution = "Data provided by Discogs"

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
        self, discogs_data: dict, name: str, description: SensorEntityDescription
    ) -> None:
        """Initialize the Discogs sensor."""
        self.entity_description = description
        self._discogs_data = discogs_data
        self._attrs: dict = {}
        self._attr_name = f"{name} {description.name}"

        if description.key in [
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ]:
            self._attr_native_unit_of_measurement = self._discogs_data["currency_symbol"]

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return the device state attributes of the sensor."""
        if self._attr_native_value is None:
            return None

        attributes: dict = {ATTR_IDENTITY: self._discogs_data["user"]}

        if self.entity_description.key == SENSOR_RANDOM_RECORD_TYPE and self._attrs:
            first_format = self._attrs.get("formats", [{}])[0]
            format_name = first_format.get("name")
            descriptions = first_format.get("descriptions", [])

            format_str = None
            if format_name:
                format_parts = [format_name]
                if descriptions:
                    format_parts.append(f"({', '.join(descriptions)})")
                format_str = " ".join(format_parts)

            first_label = self._attrs.get("labels", [{}])[0]

            # FIX CVE-008: Only expose cover_image URL if it points to a trusted domain
            raw_cover_url = self._attrs.get("cover_image")
            safe_cover_url = raw_cover_url if _is_trusted_image_url(raw_cover_url) else None
            if raw_cover_url and not safe_cover_url:
                _LOGGER.warning(
                    "Discarded untrusted cover_image URL (domain not in allowlist): %s",
                    raw_cover_url,
                )

            attributes.update(
                {
                    "cat_no": first_label.get("catno"),
                    "cover_image": safe_cover_url,
                    "format": format_str,
                    "label": first_label.get("name"),
                    "released": self._attrs.get("year"),
                }
            )
        return attributes

    def get_random_record(self) -> str | None:
        """Get a random record suggestion from the user's collection."""
        folders = self._discogs_data.get("folders", [])
        if folders and folders[0].count > 0:
            collection = folders[0]
            random_index = random.randrange(collection.count)
            random_record = collection.releases[random_index].release
            self._attrs = random_record.data
            _LOGGER.debug("Fetched random record data (title: %s)", self._attrs.get("title"))
            artist_name = (
                self._attrs.get("artists", [{}])[0].get("name")
                if self._attrs.get("artists")
                else "Unknown Artist"
            )
            title = self._attrs.get("title", "Unknown Title")
            return f"{artist_name} - {title}"
        _LOGGER.debug("No folders or empty first folder; cannot get random record.")
        return None

    def update(self) -> None:
        """Set state to the amount of records or collection value."""
        _LOGGER.debug("Updating Discogs sensor: %s", self.entity_description.key)

        key = self.entity_description.key

        if key == SENSOR_COLLECTION_TYPE:
            self._attr_native_value = self._discogs_data["collection_count"]
        elif key == SENSOR_WANTLIST_TYPE:
            self._attr_native_value = self._discogs_data["wantlist_count"]
        elif key == SENSOR_VINYL_COUNT_TYPE:
            self._attr_native_value = self._discogs_data.get(SENSOR_VINYL_COUNT_TYPE, 0)
        elif key == SENSOR_CD_COUNT_TYPE:
            self._attr_native_value = self._discogs_data.get(SENSOR_CD_COUNT_TYPE, 0)
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
                # Strip thousands separators then remove all non-numeric characters
                # except the decimal point, so "€1,792.50" → "1792.50"
                numeric_value_str = re.sub(
                    r"[^\d.]", "", value_str.replace(",", "")
                )
                if numeric_value_str:
                    try:
                        # FIX CVE-007: Removed the erroneous * 1000 / 1000 no-op
                        self._attr_native_value = float(numeric_value_str)
                        # FIX CVE-010: Value is financial PII — log at DEBUG only
                        _LOGGER.debug(
                            "Collection value sensor '%s' updated.", data_key
                        )
                    except ValueError as err:
                        _LOGGER.error(
                            "Could not parse collection value for '%s': %s",
                            data_key,
                            err,
                        )
                        self._attr_native_value = None
                else:
                    self._attr_native_value = None
            else:
                self._attr_native_value = None
        else:
            self._attr_native_value = self.get_random_record()
