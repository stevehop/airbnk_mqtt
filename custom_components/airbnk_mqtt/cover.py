"""Cover platform for Airbnk MQTT integration (modern HA API)."""
from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, AIRBNK_DEVICES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Airbnk cover entity."""
    devices = hass.data[DOMAIN].get(AIRBNK_DEVICES, {})
    _LOGGER.debug("airbnk_mqtt/cover: devices visible = %d", len(devices))

    entities: list[AirbnkCover] = []
    for dev_id, device in devices.items():
        entities.append(AirbnkCover(hass, device))

    _LOGGER.debug("airbnk_mqtt/cover: adding %d cover entities", len(entities))
    if entities:
        async_add_entities(entities)


class AirbnkCover(CoverEntity):
    """Representation of the Airbnk (Parcel Box) lock as a cover."""

    _attr_should_poll = False
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    # OPTIONAL while testing (shows both buttons regardless of state):
    # _attr_assumed_state = True

    def __init__(self, hass: HomeAssistant, device) -> None:
        self.hass = hass
        self._device = device
        self._unsubscribe: Callable[[], None] | None = None

        dev_name = device._lockConfig.get("deviceName", "Airbnk Lock")
        self._attr_name = f"{dev_name} Cover"
        self._attr_unique_id = f'{device._lockConfig.get("sn","unknown")}_cover'

    @property
    def device_info(self) -> dict | None:
        """Attach to the same HA device as the sensors."""
        return getattr(self._device, "device_info", None)

    async def async_added_to_hass(self) -> None:
        def _cb():
            self.async_write_ha_state()

        if hasattr(self._device, "register_callback"):
            self._device.register_callback(_cb)
            if hasattr(self._device, "deregister_callback"):
                self._unsubscribe = lambda: self._device.deregister_callback(_cb)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            try:
                self._unsubscribe()
            except Exception:
                pass

    # -------- STATE --------
    @property
    def available(self) -> bool:
        return getattr(self._device, "is_available", True)

    @property
    def is_closed(self) -> bool | None:
        """
        Closed when 'locked'; open when 'unlocked'.
        Prefer the parsed text state that your sensors use to avoid inversion.
        """
        # 1) Authoritative parsed text state
        data = getattr(self._device, "_lockData", {})
        text = (data.get("state") or "").strip().lower()
        if text in ("locked", "unlocked"):
            return text == "locked"

        # 2) Fallback to string current_state (if present)
        current_state = getattr(self._device, "current_state", None)
        if isinstance(current_state, str) and current_state:
            cs = current_state.strip().lower()
            if cs in ("locked", "unlocked"):
                return cs == "locked"

        # 3) Fallback to numeric curr_state (0=locked, 1=unlocked)
        state_num = getattr(self._device, "curr_state", None)
        if state_num in (0, 1):
            return state_num == 0

        # Unknown
        return None

    # -------- COMMANDS --------
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open cover = Unlock the Airbnk lock."""
        try:
            await self._device.operateLock(1)  # 1 = unlock direction
        except Exception as err:
            _LOGGER.error("Failed to unlock Airbnk device: %s", err)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close cover = Lock the Airbnk lock."""
        try:
            await self._device.operateLock(0)  # 0 = lock direction
        except Exception as err:
            _LOGGER.error("Failed to lock Airbnk device: %s", err)
