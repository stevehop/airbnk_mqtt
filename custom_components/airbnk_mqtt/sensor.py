"""Sensors for Airbnk MQTT integration (modern HA enums/units) with diagnostics."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import PERCENTAGE, UnitOfElectricPotential, UnitOfTime

from .const import (
    DOMAIN,
    AIRBNK_DEVICES,
    SENSOR_TYPE_STATE,
    SENSOR_TYPE_BATTERY,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_LAST_ADVERT,
    SENSOR_TYPE_LOCK_EVENTS,
    SENSOR_TYPE_SIGNAL_STRENGTH,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Airbnk sensors from a config entry."""
    devices = hass.data[DOMAIN].get(AIRBNK_DEVICES, {})
    _LOGGER.debug("airbnk_mqtt/sensor: devices visible to sensor platform = %d", len(devices))

    entities: list[AirbnkSensor] = []
    for dev_id, device in devices.items():
        _LOGGER.debug("airbnk_mqtt/sensor: building entities for device %s", dev_id)
        entities.append(AirbnkTextSensor(hass, device, SENSOR_TYPE_STATE))
        entities.append(AirbnkBatteryPercentSensor(hass, device, SENSOR_TYPE_BATTERY))
        entities.append(AirbnkVoltageSensor(hass, device, SENSOR_TYPE_VOLTAGE))
        entities.append(AirbnkLastAdvertSensor(hass, device, SENSOR_TYPE_LAST_ADVERT))
        entities.append(AirbnkSignalStrengthSensor(hass, device, SENSOR_TYPE_SIGNAL_STRENGTH))
        entities.append(AirbnkLockEventsSensor(hass, device, SENSOR_TYPE_LOCK_EVENTS))

    _LOGGER.debug("airbnk_mqtt/sensor: adding %d sensor entities", len(entities))
    if entities:
        async_add_entities(entities)


class AirbnkSensor(SensorEntity):
    """Base class for Airbnk sensors."""
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, device, key: str) -> None:
        self.hass = hass
        self._device = device
        self._key = key
        self._unsubscribe: Callable[[], None] | None = None

        dev_name = self._device._lockConfig.get("deviceName", "Airbnk Lock")
        self._attr_name = f"{dev_name} {self._friendly_name_for_key(key)}"
        self._attr_unique_id = f'{self._device._lockConfig.get("sn","unknown")}_{key}'

    @property
    def device_info(self) -> dict | None:
        """Attach entities to the same device in the registry."""
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

    @property
    def available(self) -> bool:
        return getattr(self._device, "is_available", True)

    @property
    def native_value(self) -> Any:
        data = getattr(self._device, "_lockData", {})
        return data.get(self._key)

    def _friendly_name_for_key(self, key: str) -> str:
        mapping = {
            SENSOR_TYPE_STATE: "Status",
            SENSOR_TYPE_BATTERY: "Battery",
            SENSOR_TYPE_VOLTAGE: "Battery Voltage",
            SENSOR_TYPE_LAST_ADVERT: "Time from Last Advert",
            SENSOR_TYPE_SIGNAL_STRENGTH: "Signal Strength",
            SENSOR_TYPE_LOCK_EVENTS: "Lock Events Counter",
        }
        return mapping.get(key, key)


class AirbnkTextSensor(AirbnkSensor):
    """State / text-like sensor (no device class)."""
    # No device_class; shows as text


class AirbnkBatteryPercentSensor(AirbnkSensor):
    @property
    def device_class(self) -> Optional[SensorDeviceClass]:
        return SensorDeviceClass.BATTERY  # platform enum

    @property
    def native_unit_of_measurement(self) -> str:
        return PERCENTAGE


class AirbnkVoltageSensor(AirbnkSensor):
    @property
    def device_class(self) -> Optional[SensorDeviceClass]:
        return SensorDeviceClass.VOLTAGE  # platform enum

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfElectricPotential.VOLT  # unit enum


class AirbnkLastAdvertSensor(AirbnkSensor):
    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfTime.SECONDS  # unit enum


class AirbnkSignalStrengthSensor(AirbnkSensor):
    @property
    def device_class(self) -> Optional[SensorDeviceClass]:
        return SensorDeviceClass.SIGNAL_STRENGTH  # platform enum

    @property
    def native_unit_of_measurement(self) -> str:
        # No UnitOfSignalStrength in HA; literal "dBm" is acceptable with this device_class
        return "dBm"


class AirbnkLockEventsSensor(AirbnkSensor):
    """Counter-style sensor; no device class by default."""
    # You can set state_class if you want long-term statistics
