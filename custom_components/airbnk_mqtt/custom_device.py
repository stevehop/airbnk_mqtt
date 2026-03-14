from __future__ import annotations

import json
import time
from typing import Callable, Set, Optional

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import (
    DOMAIN as AIRBNK_DOMAIN,
    SENSOR_TYPE_STATE,
    SENSOR_TYPE_BATTERY,
    SENSOR_TYPE_BATTERY_LOW,
    SENSOR_TYPE_VOLTAGE,
    SENSOR_TYPE_LAST_ADVERT,
    SENSOR_TYPE_LOCK_EVENTS,
    SENSOR_TYPE_SIGNAL_STRENGTH,
    LOCK_STATE_LOCKED,
    LOCK_STATE_UNLOCKED,
    LOCK_STATE_JAMMED,
    LOCK_STATE_OPERATING,
    LOCK_STATE_FAILED,
    LOCK_STATE_STRINGS,
    CONF_MAC_ADDRESS,
    CONF_MQTT_TOPIC,
    CONF_VOLTAGE_THRESHOLDS,
    CONF_RETRIES_NUM,
    DEFAULT_RETRIES_NUM,
)

from .codes_generator import AirbnkCodesGenerator
from .airbnk_logger import AirbnkLogger


# Seconds after which the device is marked unavailable if no ADV/TELE is received
MAX_NORECEIVE_TIME = 30

# MQTT topics (base replaced with self._config_topic)
BLETelemetryTopic = "%s/tele"
BLEOpTopic = "%s/command"
BLEStateTopic = "%s/adv"
BLEOperationReportTopic = "%s/command_result"


class CustomMqttLockDevice:
    """
    Runtime representation of a single Airbnk lock, tied to one HA config entry.
    Parses ADV frames, exposes telemetry, sends unlock/lock commands, and
    publishes a canonical string state for entities to consume.
    """

    # frequently updated fields
    utcMinutes: Optional[int] = None
    voltage: Optional[float] = None
    isBackLock: Optional[bool] = None
    isInit: Optional[bool] = None
    isImageA: Optional[bool] = None
    isHadNewRecord: Optional[bool] = None
    curr_state: int = LOCK_STATE_UNLOCKED
    softVersion: Optional[str] = None
    isEnableAuto: Optional[bool] = None
    opensClockwise: Optional[bool] = None
    isLowBattery: Optional[bool] = None
    magnetcurr_state: Optional[int] = None
    isMagnetEnable: Optional[bool] = None
    isBABA: Optional[bool] = None
    lversionOfSoft: Optional[int] = None
    versionOfSoft: Optional[int] = None
    versionCode: Optional[int] = None
    serialnumber: Optional[str] = None
    lockEvents: int = 0

    # integration bookkeeping
    _lockConfig: dict = {}
    _lockData: dict = {}
    _codes_generator: Optional[AirbnkCodesGenerator] = None
    cmd: dict = {}
    last_advert_time: int = 0
    last_telemetry_time: int = 0
    is_available: bool = False
    retries_num: int = DEFAULT_RETRIES_NUM
    curr_try: int = 0

    # HA callback management
    _callbacks: Set[Callable[[], None]]
    _unsubscribe_callbacks: Set[Callable[[], None]]

    # base topic used everywhere
    _config_topic: str = "parcel-box"

    def __init__(self, hass: HomeAssistant, device_config: dict, entry_options: dict):
        self.logger = AirbnkLogger(__name__)
        self.logger.debug("Setting up CustomMqttLockDevice for sn %s", device_config.get("sn"))

        self.hass = hass
        self._callbacks = set()
        self._unsubscribe_callbacks = set()

        self._lockConfig = device_config
        self._codes_generator = AirbnkCodesGenerator()
        self._lockData = self._codes_generator.decryptKeys(
            device_config["newSninfo"], device_config["appKey"]
        )

        # Determine the MQTT device base topic in a forward/backward compatible way
        # Prefer the config flow's CONF_MQTT_TOPIC; fallback to historical "deviceTopic" or a sane default.
        self._config_topic = (
            device_config.get(CONF_MQTT_TOPIC)
            or device_config.get("deviceTopic")
            or "parcel-box"
        )

        self.set_options(entry_options)
        self.logger.debug("...done")

    # -------------------- HA device info (for Device Registry) --------------------

    @property
    def device_info(self):
        """Return a device description for device registry (attaches entities to the same device)."""
        devID = self._lockData.get("lockSn", self._lockConfig.get("sn", "unknown"))
        mac = self._lockConfig.get(CONF_MAC_ADDRESS)
        info = {
            "identifiers": {(AIRBNK_DOMAIN, devID)},
            "manufacturer": "Airbnk",
            "model": self._lockConfig.get("deviceType", "Unknown"),
            "name": self._lockConfig.get("deviceName", "Parcel Box"),
            "sw_version": self._lockConfig.get("firmwareVersion"),
            "connections": {(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
        }
        return info

    # -------------------- Availability & options --------------------

    def check_availability(self):
        """Mark device unavailable if no telemetry/adv has been received recently."""
        curr_time = int(round(time.time()))
        deltatime1 = curr_time - self.last_advert_time
        deltatime2 = curr_time - self.last_telemetry_time
        if min(deltatime1, deltatime2) >= MAX_NORECEIVE_TIME:
            self.is_available = False

    def set_options(self, entry_options: dict):
        """Receive/Apply options from the options flow."""
        self.logger.debug("Options set: %s", entry_options)
        self.retries_num = entry_options.get(CONF_RETRIES_NUM, DEFAULT_RETRIES_NUM)

    # -------------------- MQTT subscription wiring --------------------

    async def mqtt_subscribe(self):
        """Subscribe to ADV, TELE, and command result topics for this device."""
        if "mqtt" not in self.hass.data:
            self.logger.error(
                "MQTT is not connected: cannot subscribe. Have you configured an MQTT Broker?"
            )
            return

        adv_topic = BLEStateTopic % self._config_topic
        tele_topic = BLETelemetryTopic % self._config_topic
        result_topic = BLEOperationReportTopic % self._config_topic

        @callback
        async def adv_received(msg) -> None:
            self.parse_adv_message(msg.payload)

        @callback
        async def telemetry_msg_received(msg) -> None:
            self.parse_telemetry_message(msg.payload)

        @callback
        async def operation_msg_received(msg) -> None:
            self.parse_operation_message(msg.payload)

        # Subscribe + keep unsubs to remove later on unload
        cb = await mqtt.async_subscribe(self.hass, adv_topic, msg_callback=adv_received)
        self._unsubscribe_callbacks.add(cb)

        cb = await mqtt.async_subscribe(self.hass, tele_topic, msg_callback=telemetry_msg_received)
        self._unsubscribe_callbacks.add(cb)

        cb = await mqtt.async_subscribe(self.hass, result_topic, msg_callback=operation_msg_received)
        self._unsubscribe_callbacks.add(cb)

    async def mqtt_unsubscribe(self):
        """Unsubscribe all MQTT handlers registered by this instance."""
        for callback_func in list(self._unsubscribe_callbacks):
            try:
                callback_func()
            except Exception:
                pass
        self._unsubscribe_callbacks.clear()

    # -------------------- Entity change callbacks --------------------

    def register_callback(self, callback: Callable[[], None]) -> None:
        """Register callback, called when device state changes."""
        self._callbacks.add(callback)

    def deregister_callback(self, callback: Callable[[], None]) -> None:
        """Deregister a previously registered callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    # -------------------- Incoming MQTT parsers --------------------

    def parse_telemetry_message(self, msg: str):
        """Handle generic telemetry; marks device as available."""
        self.logger.debug("Received telemetry %s", msg)
        self.last_telemetry_time = int(round(time.time()))
        self.is_available = True
        # Extend here if you later publish structured telemetry to /tele

    def parse_adv_message(self, msg: str):
        """Handle ADV payloads (JSON) emitted by your ESPHome gateway."""
        self.logger.debug("Received adv %s", msg)
        payload = json.loads(msg)

        mac_address = self._lockConfig.get(CONF_MAC_ADDRESS)
        mqtt_mac = payload["mac"].replace(":", "").upper()
        cfg_mac = (mac_address or "").upper()

        self.logger.debug("Config mac %s, received %s", cfg_mac, mqtt_mac)
        if mqtt_mac != cfg_mac:
            return  # ignore other devices

        mqtt_advert = payload["data"]
        self.parse_MQTT_advert(mqtt_advert.upper())

        previous = self.last_advert_time
        self.last_advert_time = int(round(time.time()))
        if "rssi" in payload:
            rssi = payload["rssi"]
            self._lockData[SENSOR_TYPE_SIGNAL_STRENGTH] = rssi

        # Delta time from previous advert for Last Advert sensor
        deltatime = self.last_advert_time - previous if previous else 0
        self._lockData[SENSOR_TYPE_LAST_ADVERT] = deltatime

        self.is_available = True
        self.logger.debug("Time from last message: %s secs", str(deltatime))

        for callback_func in list(self._callbacks):
            try:
                callback_func()
            except Exception:
                pass

    def parse_operation_message(self, msg: str):
        """Handle command_result payload after sending an operation (unlock/lock)."""
        self.logger.debug("Received operation result %s", msg)
        payload = json.loads(msg)

        mac_address = self._lockConfig.get(CONF_MAC_ADDRESS)
        mqtt_mac = payload["mac"].replace(":", "").upper()
        cfg_mac = (mac_address or "").upper()
        if mqtt_mac != cfg_mac:
            return

        msg_sign = payload.get("sign")
        self.logger.debug("Received sign: %s", msg_sign)
        self.logger.debug("Command sign: %s", self.cmd.get("sign"))

        if msg_sign != self.cmd.get("sign"):
            # Ignore results for other commands
            self.logger.error("Command signature does not match; ignoring result.")
            return

        success = payload.get("success", False)
        if not success:
            # Retry loop
            if self.curr_try < self.retries_num:
                self.curr_try += 1
                time.sleep(0.5)
                self.logger.debug("Retrying: attempt %i", self.curr_try)
                self.curr_state = LOCK_STATE_OPERATING
                for cb in list(self._callbacks):
                    try:
                        cb()
                    except Exception:
                        pass
                self.send_mqtt_command()
            else:
                self.logger.error("No more retries: command FAILED")
                self.curr_state = LOCK_STATE_FAILED
                for cb in list(self._callbacks):
                    try:
                        cb()
                    except Exception:
                        pass
                raise Exception(f"Failed sending command: returned {success}")
            return

        # Successful result → lockStatus is hex string
        lock_status = payload.get("lockStatus")
        if lock_status:
            self.parse_new_lockStatus(lock_status)

        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:
                pass

    # -------------------- Outgoing command helpers --------------------

    async def operateLock(self, lock_dir: int):
        """
        Perform a lock/unlock operation.
        lock_dir: 1 = unlock ; 0 = lock
        """
        self.logger.debug("operateLock called (%s)", lock_dir)
        self.curr_state = LOCK_STATE_OPERATING
        self.curr_try = 0
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:
                pass

        opCode = self._codes_generator.generateOperationCode(lock_dir, self.lockEvents)
        # The device expects these two writes split over command1/command2 segments
        self.cmd = {
            "command1": "FF00" + opCode[0:36].decode("utf-8"),
            "command2": "FF01" + opCode[36:].decode("utf-8"),
            "sign": self._codes_generator.systemTime,
        }
        self.send_mqtt_command()

    def send_mqtt_command(self):
        """Publish the command payload to the device command topic."""
        mqtt.publish(
            self.hass,
            BLEOpTopic % self._config_topic,
            json.dumps(self.cmd),
        )

    # -------------------- State parsing & normalization --------------------

    def parse_new_lockStatus(self, lockStatus: str):
        """
        Parse the immediate lock status (hex) returned after a command,
        normalize numeric state, then publish canonical text state.
        """
        self.logger.debug("Parsing new lockStatus: %s", lockStatus)
        try:
            bArr = bytearray.fromhex(lockStatus)
        except Exception:
            self.logger.error("Bad lockStatus payload: %s", lockStatus)
            return

        # minimal sanity check (mirrors your earlier structure)
        if len(bArr) < 20 or bArr[0] != 0xAA or bArr[3] != 0x02 or bArr[4] != 0x04:
            self.logger.error("Wrong lockStatus msg: %s", lockStatus)
            return

        # some devices encode events and voltage here too (keep if useful)
        self.lockEvents = max(self.lockEvents, (bArr[10] << 24) | (bArr[11] << 16) | (bArr[12] << 8) | bArr[13])
        self.voltage = float(((bArr[14] << 8) | bArr[15]) * 0.01)

        # raw state (upper nibble of byte 16)
        raw_state = (bArr[16] >> 4) & 0x03  # 0..3
        self.curr_state = raw_state

        # Apply direction flip if needed (keep jammed state intact)
        if self.curr_state != LOCK_STATE_JAMMED and self.opensClockwise:
            self.curr_state = 1 - self.curr_state  # 0<->1

        # Publish canonical text state & sensors
        self._publish_canonical_text_state()

    def parse_MQTT_advert(self, mqtt_advert: str):
        """
        Parse BABA manufacturer ADV payload (hex string) emitted by ESPHome gateway.
        Update numeric state, apply direction flip (opensClockwise), then publish
        canonical text state and other sensors.
        """
        self.logger.debug("Parsing advert msg: %s", mqtt_advert)
        try:
            bArr = bytearray.fromhex(mqtt_advert)
        except Exception:
            self.logger.error("Wrong advert msg: %s", mqtt_advert)
            return

        if len(bArr) < 24 or bArr[0] != 0xBA or bArr[1] != 0xBA:
            self.logger.error("Wrong advert msg: %s", mqtt_advert)
            return

        # voltage, versions, serial, events
        self.voltage = float(((bArr[16] << 8) | bArr[17]) * 0.01)
        self.boardModel = bArr[2]
        self.lversionOfSoft = bArr[3]
        self.sversionOfSoft = (bArr[4] << 16) | (bArr[5] << 8) | bArr[6]

        serialnumber = bArr[7:16].decode("utf-8", errors="ignore").strip("\0")
        if serialnumber and serialnumber != self._lockConfig.get("sn"):
            self.logger.error(
                "ERROR: s/n in advert (%s) is different from cloud data (%s)",
                serialnumber,
                self._lockConfig.get("sn"),
            )
        self.serialnumber = serialnumber or self._lockConfig.get("sn")

        lockEvents = (bArr[18] << 24) | (bArr[19] << 16) | (bArr[20] << 8) | bArr[21]
        self.lockEvents = max(self.lockEvents, lockEvents)

        # state, flags
        new_state = (bArr[22] >> 4) & 0x03
        self.opensClockwise = (bArr[22] & 0x80) != 0
        self.isBackLock = (bArr[22] & 0x01) != 0
        self.isInit = (bArr[22] & 0x02) != 0
        self.isImageA = (bArr[22] & 0x04) != 0
        self.isHadNewRecord = (bArr[22] & 0x08) != 0
        self.isEnableAuto = (bArr[22] & 0x40) != 0

        self.isLowBattery = (bArr[23] & 0x10) != 0
        self.magnetcurr_state = (bArr[23] >> 5) & 0x03
        self.isMagnetEnable = (bArr[23] & 0x80) != 0

        # numeric state with optional direction flip
        self.curr_state = new_state
        if self.curr_state != LOCK_STATE_JAMMED and self.opensClockwise:
            self.curr_state = 1 - self.curr_state  # 0<->1

        self.isBABA = True

        # Derived sensors
        self.battery_perc = self.calculate_battery_percentage(self.voltage)

        # Publish canonical state + sensors to _lockData
        self._publish_canonical_text_state()

        # Non-canonical sensors (for UI/analytics)
        self._lockData[SENSOR_TYPE_BATTERY] = self.battery_perc
        self._lockData[SENSOR_TYPE_VOLTAGE] = self.voltage
        self._lockData[SENSOR_TYPE_BATTERY_LOW] = self.isLowBattery
        self._lockData[SENSOR_TYPE_LOCK_EVENTS] = self.lockEvents

    # -------------------- Canonical state + helpers --------------------

    def _publish_canonical_text_state(self) -> None:
    """
    Publish canonical text state in _lockData[SENSOR_TYPE_STATE].
    HOT-FIX: invert 0/1 semantics so the UI matches the real latch.
    """
    value = self.curr_state
    # Invert only for normal 0/1 states; keep jammed/operating/failed untouched.
    if value in (LOCK_STATE_LOCKED, LOCK_STATE_UNLOCKED):
        value = 1 - value  # <--- HOT-FIX flip

    if value == LOCK_STATE_LOCKED:
        text_state = "locked"
    elif value == LOCK_STATE_UNLOCKED:
        text_state = "unlocked"
    elif value == LOCK_STATE_JAMMED:
        text_state = "jammed"
    elif value == LOCK_STATE_OPERATING:
        text_state = "operating"
    elif value == LOCK_STATE_FAILED:
        text_state = "failed"
    else:
        text_state = "unknown"

    self._lockData[SENSOR_TYPE_STATE] = text_state

    def calculate_battery_percentage(self, voltage: float) -> float:
        """
        Piecewise mapping from voltage to % using configured thresholds:
        thresholds: [Vmin, Vmid1, Vmax] in your config (4 values historically, we
        use the first three meaningful points).
        """
        voltages = self._lockConfig[CONF_VOLTAGE_THRESHOLDS]
        # simple 3-point mapping; clamp at 0..100
        perc = 0.0
        if voltage >= voltages[2]:
            perc = 100.0
        elif voltage >= voltages[1]:
            perc = 66.6 + 33.3 * (voltage - voltages[1]) / (voltages[2] - voltages[1])
        else:
            perc = 33.3 + 33.3 * (voltage - voltages[0]) / (voltages[1] - voltages[0])
        perc = max(0.0, perc)
        return round(perc, 1)
