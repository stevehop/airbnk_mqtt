"""Binary sensors for Airbnk MQTT integration (modern HA enums)."""
from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass

from .const import (
    DOMAIN,
    AIRBNK_DEVICES,
    SENSOR_TYPE_BATTERY_LOW,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Airbnk binary sensors from a config entry."""
    devices = hass.data[DOMAIN].get(AIRBNK_DEVICES, {})
    entities: list[AirbnkBinarySensor] = []

    for dev_id, device in devices.items():
        # Low battery (if device populates SENSOR_TYPE_BATTERY_LOW)
        entities.append(AirbnkBatteryLowBinarySensor(hass, device, SENSOR_TYPE_BATTERY_LOW))

    if entities:
        async_add_entities(entities)


class AirbnkBinarySensor(BinarySensorEntity):
    """Base class for Airbnk binary sensors."""

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, device, key: str) -> None:
        self.hass = hass
        self._device = device
        self._key = key
        self._unsubscribe: Callable[[], None] | None = None

        dev_name = self._device._lockConfig.get("deviceName", "Airbnk Lock")
        self._attr_name = f"{dev_name} {self._friendly_name_for_key(key)}"
        self._attr_unique_id = f'{self._device._lockConfig.get("sn","unknown")}_{key}'

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

    @property
    def available(self) -> bool:
        return getattr(self._device, "is_available", True)

    @property
    def is_on(self) -> bool | None:
        data = getattr(self._device, "_lockData", {})
        return bool(data.get(self._key)) if self._key in data else None

    def _friendly_name_for_key(self, key: str) -> str:
        # Minimal mapping; expand if you have more binary sensors
        if key == SENSOR_TYPE_BATTERY_LOW:
            return "Battery Low"
        return key


class AirbnkBatteryLowBinarySensor(AirbnkBinarySensor):
    """Binary sensor representing low battery condition."""

    @property
    def device_class(self) -> BinarySensorDeviceClass | None:
        return BinarySensorDeviceClass.BATTERY
``
