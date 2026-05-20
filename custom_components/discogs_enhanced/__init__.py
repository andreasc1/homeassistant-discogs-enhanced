"""The Discogs Enhanced custom integration."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "discogs_enhanced"


def setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Discogs component platform environment explicitly."""
    _LOGGER.info("Setting up Discogs Enhanced custom integration core dependencies")
    return True
