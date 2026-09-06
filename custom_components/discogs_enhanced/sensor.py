"""Show the amount of records in a user's Discogs collection and its value,
including counts categorized by media format.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import random
import re
import json
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
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import SERVER_SOFTWARE, async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

ATTR_IDENTITY = "identity"

DEFAULT_NAME = "Discogs"

ICON_RECORD = "mdi:album"
ICON_PLAYER = "mdi:record-player"
ICON_CASH = "mdi:cash"
UNIT_RECORDS = "records"

# How often the collection is re-fetched from Discogs.
#
# FIX (issue #2): This value now actually drives a real refresh. Previously all
# data was fetched a single time in async_setup_platform and the periodic
# update() calls only re-read that frozen snapshot — so counts and values never
# changed until Home Assistant was restarted. A DataUpdateCoordinator now does a
# genuine API refresh on this interval and every sensor reads from it.
SCAN_INTERVAL = timedelta(minutes=30)

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


def _fetch_identity(discogs_client_instance) -> dict:
    """FIX CVE-001: Blocking discogs_client calls isolated for executor dispatch.

    Returns the freshly-fetched identity counts plus the collection folder
    objects. Called on every coordinator refresh so num_collection /
    num_wantlist reflect the current state of the account, not a value frozen
    at startup.
    """
    identity = discogs_client_instance.identity()
    folders = identity.collection_folders
    return {
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


def _fetch_folder_details(main_folder) -> dict:
    """Single pass over the collection folder: count formats AND pick a random record.

    Runs in the executor (blocking pagination). Doing both in one pass means the
    random-record pick costs no extra API calls — its metadata comes from the
    ``basic_information`` already returned for each collection item during the
    format-count iteration, instead of a second paginated fetch.

    Uniform random selection uses reservoir sampling (k=1) so we never need to
    know the total up front or index back into the paginated list.
    """
    vinyl_count = 0
    cd_count = 0
    seen = 0
    chosen: dict | None = None

    for release_item in main_folder.releases:
        # .release is built from the item's basic_information block, so .data is
        # that dict — no per-release API call is triggered here.
        basic_info = release_item.release.data
        formats = basic_info.get("formats", [])
        if formats and formats[0].get("name"):
            primary_format = formats[0]["name"].lower()
            if primary_format == "vinyl":
                vinyl_count += 1
            elif primary_format == "cd":
                cd_count += 1

        seen += 1
        if random.randrange(seen) == 0:
            chosen = basic_info

    return {
        "vinyl_count": vinyl_count,
        "cd_count": cd_count,
        "random_attrs": chosen,
    }


def _build_random_display(basic_info: dict | None) -> str | None:
    """Build the 'Artist - Title' state string from a basic_information dict."""
    if not basic_info:
        return None
    artists = basic_info.get("artists") or []
    artist_name = artists[0].get("name") if artists else "Unknown Artist"
    title = basic_info.get("title", "Unknown Title")
    return f"{artist_name} - {title}"


class DiscogsDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch all Discogs data once per interval and share it across sensors.

    This is the core of the fix for issue #2: a single scheduled refresh keeps
    every sensor current instead of freezing everything at setup time.
    """

    def __init__(self, hass: HomeAssistant, client, token: str, name: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{name} Discogs",
            update_interval=SCAN_INTERVAL,
        )
        self._client = client
        # FIX CVE-002: token held only on the coordinator for the value-endpoint
        # Authorization header; never placed into the shared data dict.
        self._token = token
        self._session = async_get_clientsession(hass)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the full data set. Runs on SCAN_INTERVAL."""
        # Carry previous values forward so a transient error on the value or
        # folder calls doesn't flap sensors to 0 / unavailable.
        previous = self.data or {}
        data: dict[str, Any] = {
            "user": previous.get("user", "Unknown"),
            "collection_count": previous.get("collection_count", 0),
            "wantlist_count": previous.get("wantlist_count", 0),
            "collection_value_min": previous.get("collection_value_min", "0.00"),
            "collection_value_median": previous.get("collection_value_median", "0.00"),
            "collection_value_max": previous.get("collection_value_max", "0.00"),
            "currency_symbol": previous.get("currency_symbol", "$"),
            SENSOR_VINYL_COUNT_TYPE: previous.get(SENSOR_VINYL_COUNT_TYPE, 0),
            SENSOR_CD_COUNT_TYPE: previous.get(SENSOR_CD_COUNT_TYPE, 0),
            "random_record": previous.get("random_record"),
            "random_attrs": previous.get("random_attrs"),
        }

        # ── Step 1: identity + counts (blocking → executor) ────────────────
        # A failure here is treated as fatal for the refresh: without identity
        # we can't trust anything, so raise UpdateFailed and let the
        # coordinator retry next interval (sensors become unavailable).
        try:
            identity = await self.hass.async_add_executor_job(
                _fetch_identity, self._client
            )
        except discogs_client.exceptions.HTTPError as err:
            raise UpdateFailed(f"Discogs API error fetching identity: {err}") from err
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Unexpected error fetching Discogs identity: {err}") from err

        currency_symbol = identity["curr_abbr"] or "$"
        data.update(
            {
                "user": identity["display_name"],
                "collection_count": identity["num_collection"],
                "wantlist_count": identity["num_wantlist"],
                "currency_symbol": currency_symbol,
            }
        )

        # ── Step 2: collection value (async aiohttp) ───────────────────────
        # Non-fatal: on error we keep the previously-known values.
        username = identity["username"]
        value_url = f"https://api.discogs.com/users/{username}/collection/value"
        headers = {
            "User-Agent": SERVER_SOFTWARE,
            "Authorization": f"Discogs token={self._token}",  # FIX CVE-002
        }
        try:
            async with self._session.get(
                value_url,
                headers=headers,
                timeout=_HTTP_TIMEOUT,
                ssl=True,  # FIX CVE-005: explicit TLS verification
            ) as response:
                response.raise_for_status()
                collection_value_raw = await response.json(content_type=None)

            if isinstance(collection_value_raw, dict) and collection_value_raw:
                data["collection_value_min"] = collection_value_raw.get("minimum", "0.00")
                data["collection_value_median"] = collection_value_raw.get("median", "0.00")
                data["collection_value_max"] = collection_value_raw.get("maximum", "0.00")
            else:
                _LOGGER.warning(
                    "Discogs /collection/value returned unexpected data; keeping previous values."
                )
        except aiohttp.ClientSSLError:
            _LOGGER.error(
                "TLS certificate verification failed for Discogs API. "
                "Possible MITM or misconfigured CA store; keeping previous values."
            )
        except aiohttp.ClientResponseError as err:
            _LOGGER.warning(
                "HTTP error fetching collection value (status %s); keeping previous values.",
                err.status,
            )
        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "Network error fetching collection value: %s; keeping previous values.", err
            )
        except (json.JSONDecodeError, ValueError) as err:
            _LOGGER.warning(
                "Could not parse collection value response: %s; keeping previous values.", err
            )

        # ── Step 3: format counts + random record (blocking → executor) ────
        # Non-fatal: on error we keep the previously-known counts/record.
        folders = identity["folders"]
        if folders and data["collection_count"] > 0:
            try:
                details = await self.hass.async_add_executor_job(
                    _fetch_folder_details, folders[0]
                )
                data[SENSOR_VINYL_COUNT_TYPE] = details["vinyl_count"]
                data[SENSOR_CD_COUNT_TYPE] = details["cd_count"]
                data["random_attrs"] = details["random_attrs"]
                data["random_record"] = _build_random_display(details["random_attrs"])
            except discogs_client.exceptions.HTTPError as err:
                _LOGGER.error(
                    "Rate limit or permission error reading collection folder: %s; "
                    "keeping previous counts/record.", err
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error reading collection folder; keeping previous counts/record."
                )
        elif data["collection_count"] == 0:
            data[SENSOR_VINYL_COUNT_TYPE] = 0
            data[SENSOR_CD_COUNT_TYPE] = 0
            data["random_attrs"] = None
            data["random_record"] = None

        return data


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Discogs sensor platform via a shared update coordinator."""
    token = config[CONF_TOKEN]
    name = config[CONF_NAME]

    _LOGGER.debug(
        "Setting up Discogs Enhanced sensor platform (token: %s).",
        _mask_token(token),  # FIX CVE-002: never log the raw token
    )

    _discogs_client = discogs_client.Client(SERVER_SOFTWARE, user_token=token)

    coordinator = DiscogsDataUpdateCoordinator(hass, _discogs_client, token, name)

    # First fetch before entities are added. If it fails, raise PlatformNotReady
    # so HA retries setup with backoff instead of loading dead sensors.
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise PlatformNotReady("Initial Discogs fetch failed")

    monitored_conditions = config[CONF_MONITORED_CONDITIONS]
    entities = [
        DiscogsSensor(coordinator, name, description)
        for description in SENSOR_TYPES
        if description.key in monitored_conditions
    ]

    async_add_entities(entities)


class DiscogsSensor(CoordinatorEntity[DiscogsDataUpdateCoordinator], SensorEntity):
    """A Discogs sensor backed by the shared update coordinator."""

    _attr_attribution = "Data provided by Discogs"

    def __init__(
        self,
        coordinator: DiscogsDataUpdateCoordinator,
        name: str,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the Discogs sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_name = f"{name} {description.name}"

        if description.key in (
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ):
            self._attr_native_unit_of_measurement = coordinator.data["currency_symbol"]

    @property
    def device_class(self):
        """Return the device class."""
        if self.entity_description.key in (
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ):
            return "monetary"
        return None

    @property
    def native_value(self):
        """Return the current value for this sensor, derived from coordinator data."""
        data = self.coordinator.data
        key = self.entity_description.key

        if key == SENSOR_COLLECTION_TYPE:
            return data["collection_count"]
        if key == SENSOR_WANTLIST_TYPE:
            return data["wantlist_count"]
        if key == SENSOR_VINYL_COUNT_TYPE:
            return data.get(SENSOR_VINYL_COUNT_TYPE, 0)
        if key == SENSOR_CD_COUNT_TYPE:
            return data.get(SENSOR_CD_COUNT_TYPE, 0)
        if key in (
            SENSOR_COLLECTION_VALUE_MIN_TYPE,
            SENSOR_COLLECTION_VALUE_MEDIAN_TYPE,
            SENSOR_COLLECTION_VALUE_MAX_TYPE,
        ):
            key_map = {
                SENSOR_COLLECTION_VALUE_MIN_TYPE: "collection_value_min",
                SENSOR_COLLECTION_VALUE_MEDIAN_TYPE: "collection_value_median",
                SENSOR_COLLECTION_VALUE_MAX_TYPE: "collection_value_max",
            }
            value_str = data.get(key_map[key])
            if isinstance(value_str, str) and value_str:
                # "€1,792.50" → "1792.50"
                numeric_value_str = re.sub(r"[^\d.]", "", value_str.replace(",", ""))
                if numeric_value_str:
                    try:
                        return float(numeric_value_str)  # FIX CVE-007
                    except ValueError as err:
                        _LOGGER.error("Could not parse collection value: %s", err)
                        return None
            return None
        # Random record
        return data.get("random_record")

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return the state attributes of the sensor."""
        if self.native_value is None:
            return None

        data = self.coordinator.data
        attributes: dict = {ATTR_IDENTITY: data["user"]}

        if self.entity_description.key == SENSOR_RANDOM_RECORD_TYPE:
            attrs = data.get("random_attrs")
            if attrs:
                first_format = (attrs.get("formats") or [{}])[0]
                format_name = first_format.get("name")
                descriptions = first_format.get("descriptions", [])

                format_str = None
                if format_name:
                    format_parts = [format_name]
                    if descriptions:
                        format_parts.append(f"({', '.join(descriptions)})")
                    format_str = " ".join(format_parts)

                first_label = (attrs.get("labels") or [{}])[0]

                # FIX CVE-008: only expose cover_image from a trusted domain
                raw_cover_url = attrs.get("cover_image")
                safe_cover_url = raw_cover_url if _is_trusted_image_url(raw_cover_url) else None
                if raw_cover_url and not safe_cover_url:
                    _LOGGER.warning(
                        "Discarded untrusted cover_image URL (not in allowlist): %s",
                        raw_cover_url,
                    )

                attributes.update(
                    {
                        "cat_no": first_label.get("catno"),
                        "cover_image": safe_cover_url,
                        "format": format_str,
                        "label": first_label.get("name"),
                        "released": attrs.get("year"),
                    }
                )
        return attributes
