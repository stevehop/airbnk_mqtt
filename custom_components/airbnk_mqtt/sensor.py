"""Airbnk sensors with corrected lock state + accurate MQTT battery telemetry."""

from __future__ import annotations
import logging
from typing import Any, Optional, Callable

from homeassistant.helpers.entity import Entity
from homeassistant.components.sensor import SensorDeviceClass

from homeassistant.const import (
    CONF_ICON,
    CONF_NAME,
    CONF_TYPE,
    CONF_UNIT_OF_MEASUREMENT,
)

from .const import (
    DOMAIN as AIRBNK_DOMAIN,
    AIRBNK_DEVICES,
    SENSOR_TYPE_STATE,
    SENSOR_TYPE_BATTERY,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_LAST_ADVERT,
    SENSOR_TYPE_LOCK_EVENTS,
    SENSOR_TYPE_SIGNAL_STRENGTH,
    SENSOR_TYPES,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Deprecated classic setup."""
    return


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up sensor entities from config entry."""
    devices = hass.data[AIRBNK_DOMAIN][AIRBNK_DEVICES]
    entities = []

    for dev_id, device in devices.items():
        # Legacy Airbnk sensors (some overridden)
        for monitored_attr in SENSOR_TYPES:
            entities.append(AirbnkSensor.factory(hass, device, monitored_attr))

        # Accurate MQTT battery sensors from ESPHome
        entities.append(AirbnkMQTTBatteryPercent(hass, device))
        entities.append(AirbnkMQTTBatteryVoltage(hass, device))

    async_add_entities(entities, update_before_add=True)


# ---------------------------------------------------------------------------
# Base Sensor
# ---------------------------------------------------------------------------

class AirbnkSensor(Entity):
    """Base class for all Airbnk sensors."""

    @property
    def should_poll(self):
        return False

    @staticmethod
    def factory(hass, device, attr):
        mapping = {
            SENSOR_TYPE_STATE: AirbnkStateSensor,
            SENSOR_TYPE_BATTERY: AirbnkLegacyBatterySensor,
            SENSOR_TYPE_VOLTAGE: AirbnkTextSensor,
            SENSOR_TYPE_SIGNAL_STRENGTH: AirbnkTextSensor,
            SENSOR_TYPE_LAST_ADVERT: AirbnkTextSensor,
            SENSOR_TYPE_LOCK_EVENTS: AirbnkTextSensor,
        }
        cls = mapping[SENSOR_TYPES[attr][CONF_TYPE]]
        return cls(hass, device, attr)

    def __init__(self, hass, device, attr):
        self.hass = hass
        self._device = device
        self._attr = attr
        self._sensor_def = SENSOR_TYPES[attr]
        devname = self._device._lockConfig.get("deviceName", "Airbnk Lock")
        self._name = f"{devname} {self._sensor_def[CONF_NAME]}"
        self._unsubscribe = None

    async def async_added_to_hass(self):
        """Register callback for push updates."""
        def _cb():
            self.async_write_ha_state()

        if hasattr(self._device, "register_callback"):
            self._device.register_callback(_cb)
            if hasattr(self._device, "deregister_callback"):
                self._unsubscribe = lambda: self._device.deregister_callback(_cb)

    async def async_will_remove_from_hass(self):
        if self._unsubscribe:
            try:
                self._unsubscribe()
            except Exception:
                pass

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        sn = self._device._lockConfig.get("sn", "unknown")
        return f"{sn}_{self._attr}"

    @property
    def available(self):
        return getattr(self._device, "is_available", True)

    @property
    def icon(self):
        return self._sensor_def.get(CONF_ICON)

    @property
    def device_class(self):
        return self._sensor_def.get("SensorDeviceClass.DEVICE_CLASS")

    @property
    def unit_of_measurement(self):
        return self._sensor_def.get(CONF_UNIT_OF_MEASUREMENT)

    @property
    def device_info(self):
        return getattr(self._device, "device_info", None)

    def _raw(self):
        data = getattr(self._device, "_lockData", {})
        return data.get(self._attr)


# ---------------------------------------------------------------------------
# Legacy sensors (kept for compatibility)
# ---------------------------------------------------------------------------

class AirbnkTextSensor(AirbnkSensor):
    @property
    def state(self):
        return self._raw()


class AirbnkLegacyBatterySensor(AirbnkSensor):
    @property
    def state(self):
        return self._raw()


# ---------------------------------------------------------------------------
# Corrected State Sensor
# ---------------------------------------------------------------------------

class AirbnkStateSensor(AirbnkSensor):
    """Fix the long-standing inverted state problem."""

    @property
    def state(self):
        raw = self._raw()
        if raw is None:
            return None

        s = str(raw).strip().lower()
        if s == "locked":
            return "unlocked"
        if s == "unlocked":
            return "locked"
        return raw


# ---------------------------------------------------------------------------
# Accurate Battery Sensors (MQTT-backed)
# ---------------------------------------------------------------------------

class _AirbnkMQTTSensor(Entity):
    """Base class for battery sensors sourced from ESPHome MQTT."""

    should_poll = False

    def __init__(self, hass, device):
        self.hass = hass
        self._device = device
        self._value = None
        self._unsubscribe = None

    @property
    def device_info(self):
        return getattr(self._device, "device_info", None)

    @property
    def available(self):
        return True

    async def async_added_to_hass(self):
        """Subscribe to MQTT telemetry topic."""
        topic = self._topic

        async def _cb(msg):
            self._value = msg.payload
            self.async_write_ha_state()

        self._unsubscribe = await self.hass.components.mqtt.async_subscribe(
            topic, _cb
        )

    async def async_will_remove_from_hass(self):
        if self._unsubscribe:
            self._unsubscribe()

    @property
    def state(self):
        return self._value


class AirbnkMQTTBatteryPercent(_AirbnkMQTTSensor):
    @property
    def name(self):
        n = self._device._lockConfig.get("deviceName", "Airbnk Lock")
        return f"{n} Battery"

    @property
    def unique_id(self):
        sn = self._device._lockConfig.get("sn", "unknown")
        return f"{sn}_battpct"

    @property
    def device_class(self):
        return SensorDeviceClass.BATTERY

    @property
    def unit_of_measurement(self):
        return "%"

    @property
    def _topic(self):
        base = self._device._lockConfig.get("deviceTopic", "parcel-box")
        return f"{base}/tele/battery_percent"


class AirbnkMQTTBatteryVoltage(_AirbnkMQTTSensor):
    @property
    def name(self):
        n = self._device._lockConfig.get("deviceName", "Airbnk Lock")
        return f"{n} Battery Voltage"

    @property
    def unique_id(self):
        sn = self._device._lockConfig.get("sn", "unknown")
        return f"{sn}_battvolt"

    @property
    def device_class(self):
        return SensorDeviceClass.VOLTAGE

    @property
    def unit_of_measurement(self):
        return "V"

    @property
    def _topic(self):
        base = self._device._lockConfig.get("deviceTopic", "parcel-box")
        return f"{base}/tele/battery_voltage"
