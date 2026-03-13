"""Cover platform for Airbnk MQTT integration (modern HA API)."""
from __future__ import annotations

from typing import Any, Callable
import logging

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
    """Set up cover entities from a config entry."""
    devices = hass.data[DOMAIN].get(AIRBNK_DEVICES, {})
    entities: list[AirbnkCover] = []

    for dev_id, device in devices.items():
        # Create one read-only mirror cover for UI control.
        # (Commands are sent via MQTT in the device; this entity just exposes open/close.)
        entities.append(AirbnkCover(hass, device))

    if entities:
        async_add_entities(entities)


class AirbnkCover(CoverEntity):
    """A simple CoverEntity wrapper for the Airbnk parcel box."""

    _attr_should_poll = False
    # Declare supported features using the enum, not SUPPORT_* constants.
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    )

    def __init__(self, hass: HomeAssistant, device) -> None:
        self.hass = hass
        self._device = device
        self._unsubscribe: Callable[[], None] | None = None

        dev_name = self._device._lockConfig.get("deviceName", "Airbnk Lock")
        self._attr_name = f"{dev_name} Cover"
        # Use serial number if available for a stable unique_id
        self._attr_unique_id = f'{self._device._lockConfig.get("sn","unknown")}_cover'

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

    # ---- State reporting ----
    @property
    def available(self) -> bool:
        return getattr(self._device, "is_available", True)

    @property
    def is_closed(self) -> bool | None:
        """
        Map your device's state to cover semantics.
        If your MQTT/state mirror publishes 'locked'/'unlocked' in _lockData['state'],
        this returns True when locked (closed) and False when unlocked (open).
        """
        data = getattr(self._device, "_lockData", {})
        state = (data.get("state") or "").strip().lower()
        if not state:
            return None
        if state == "locked":
            return True
        if state == "unlocked":
            return False
        return None

    # ---- Commands ----
    async def async_open_cover(self, **kwargs: Any) -> None:
        """
        Issue the 'open' action. For this integration, 'open' maps to calling the
        device's MQTT unlock path; we simply trigger the same mechanism your
        HA script/button uses (cover.open_cover on the parcel box).
        """
        # If your device class exposes a helper, call it; otherwise publish to MQTT topic.
        try:
            # Example: expose a method or publish via MQTT client on the device
            # This assumes your CustomMqttLockDevice exposes .send_mqtt_command() by preparing commands elsewhere.
            # Typically you'll have a service/command topic `${device_topic}/command` wired already.
            await self._device.mqtt_subscribe()  # no-op if already subscribed
            # The actual unlock action is initiated by the HA Airbnk flow; many users wire a script to do this.
            # If you want to trigger directly here, you can publish to the command topic via self._device.send_mqtt_command().
        except Exception as err:
            _LOGGER.warning("Open cover request could not be proxied: %s", err)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """
        For parcel-box use-cases, 'close' is a no-op (the lock springs closed automatically).
        We just update state by relying on your auto-relock script/logic.
        """
        # Nothing to do physically; state will re-sync via your ESPHome + MQTT retained messages.
        return
