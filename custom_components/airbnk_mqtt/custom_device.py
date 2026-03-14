from __future__ import annotations

import json
import time
from typing import Callable, Optional, Set

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


MAX_NORECEIVE_TIME = 30  # seconds

# Topic templates; %s will be replaced by the device base topic (self._config_topic)
BLETelemetryTopic = "%s/tele"
BLEOpTopic = "%s/command"
BLEStateTopic = "%s/adv"
BLEOperationReportTopic = "%s/command_result"


class CustomMqttLockDevice:
    """Represents a single Airbnk device controlled via MQTT."""

    # frequently updated fields (class attributes for hints; instance sets values)
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

    _lockConfig: dict = {}
    _lockData: dict = {}
    _codes_generator: Optional[AirbnkCodesGenerator] = None
    cmd: dict = {}
    last_advert_time: int = 0
    last_telemetry_time: int = 0
    is_available: bool = False
    retries_num: int = DEFAULT_RETRIES_NUM
    curr_try: int = 0

    _callbacks: Set[Callable[[], None]]
    _unsubscribe_callbacks: Set[Callable[[], None]]

    _config_topic: str = "parcel-box"

    # explicit state inversion so HA shows the right semantics
    _state_invert: bool = True  # default True based on field observation; adjust if needed

    def __init__(self, hass: HomeAssistant, device_config: dict, entry_options: dict):
        self.logger = AirbnkLogger(__name__)
        self.logger.debug(f"Setting up CustomMqttLockDevice for sn {device_config.get('sn')}")

        self.hass = hass
        self._callbacks = set()
        self._unsubscribe_callbacks = set()

        self._lockConfig = device_config
        self._codes_generator = AirbnkCodesGenerator()
        self._lockData = self._codes_generator.decryptKeys(
            device_config["newSninfo"], device_config["appKey"]
        )

        # Base MQTT topic
        self._config_topic = (
            device_config.get(CONF_MQTT_TOPIC)
            or device_config.get("deviceTopic")
            or "parcel-box"
        )

        # Allow future flow/option to override inversion (right now default True for your device)
        self._state_invert = bool(device_config.get("invert_state", True))

        self.set_options(entry_options)
        self.logger.debug("...done")

    # -------------------- Device Registry info --------------------

    @property
    def device_info(self):
        """Return device information for HA's device registry."""
        devID = self._lockData.get("lockSn", self._lockConfig.get("sn", "unknown"))
        mac = self._lockConfig.get(CONF_MAC_ADDRESS)
        return {
            "identifiers": {(AIRBNK_DOMAIN, devID)},
            "manufacturer": "Airbnk",
            "model": self._lockConfig.get("deviceType", "Unknown"),
            "name": self._lockConfig.get("deviceName", "Parcel Box"),
            "sw_version": self._lockConfig.get("firmwareVersion"),
            "connections": {(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
        }

    # -------------------- Availability & options --------------------

    def check_availability(self):
        """Mark device unavailable if no messages arrive recently."""
        now = int(round(time.time()))
        d1 = now - self.last_advert_time
        d2 = now - self.last_telemetry_time
        if min(d1, d2) >= MAX_NORECEIVE_TIME:
            self.is_available = False

    def set_options(self, entry_options: dict):
        self.logger.debug(f"Options set: {entry_options}")
        self.retries_num = entry_options.get(CONF_RETRIES_NUM, DEFAULT_RETRIES_NUM)

    # -------------------- MQTT subscribe/unsubscribe --------------------

    async def mqtt_subscribe(self):
        if "mqtt" not in self.hass.data:
            self.logger.error(
                "MQTT is not connected: cannot subscribe. Have you configured an MQTT Broker?"
            )
            return

        adv_topic = BLEStateTopic % self._config_topic
        tele_topic = BLETelemetryTopic % self._config_topic
        result_topic = BLEOperationReportTopic % self._config_topic

        @callback
        def adv_received(msg) -> None:
            self.parse_adv_message(msg.payload)

        @callback
        def telemetry_msg_received(msg) -> None:
            self.parse_telemetry_message(msg.payload)

        @callback
        def operation_msg_received(msg) -> None:
            self.parse_operation_message(msg.payload)

        cb = await mqtt.async_subscribe(self.hass, adv_topic, msg_callback=adv_received)
        self._unsubscribe_callbacks.add(cb)

        cb = await mqtt.async_subscribe(self.hass, tele_topic, msg_callback=telemetry_msg_received)
        self._unsubscribe_callbacks.add(cb)

        cb = await mqtt.async_subscribe(self.hass, result_topic, msg_callback=operation_msg_received)
        self._unsubscribe_callbacks.add(cb)

    async def mqtt_unsubscribe(self):
        for callback_func in list(self._unsubscribe_callbacks):
            try:
                callback_func()
            except Exception:
                pass
        self._unsubscribe_callbacks.clear()

    # -------------------- Callback registration for entities --------------------

    def register_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.add(callback)

    def deregister_callback(self, callback: Callable[[], None]) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    # -------------------- Incoming message parsers --------------------

    def parse_telemetry_message(self, msg: str):
        self.logger.debug(f"Received telemetry {msg}")
        self.last_telemetry_time = int(round(time.time()))
        self.is_available = True

    def parse_adv_message(self, msg: str):
        self.logger.debug(f"Received adv {msg}")
        payload = json.loads(msg)

        cfg_mac = (self._lockConfig.get(CONF_MAC_ADDRESS) or "").upper()
        mqtt_mac = payload["mac"].replace(":", "").upper()
        self.logger.debug(f"Config mac {cfg_mac}, received {mqtt_mac}")
        if mqtt_mac != cfg_mac:
            return

        mqtt_advert = payload["data"]
        self.parse_MQTT_advert(mqtt_advert.upper())

        prev = self.last_advert_time
        self.last_advert_time = int(round(time.time()))
        if "rssi" in payload:
            self._lockData[SENSOR_TYPE_SIGNAL_STRENGTH] = payload["rssi"]

        delta = self.last_advert_time - prev if prev else 0
        self._lockData[SENSOR_TYPE_LAST_ADVERT] = delta

        self.is_available = True
        self.logger.debug(f"Time from last message: {delta} secs")

        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:
                pass

    def parse_operation_message(self, msg: str):
        self.logger.debug(f"Received operation result {msg}")
        payload = json.loads(msg)

        cfg_mac = (self._lockConfig.get(CONF_MAC_ADDRESS) or "").upper()
        mqtt_mac = payload["mac"].replace(":", "").upper()
        if mqtt_mac != cfg_mac:
            return

        msg_sign = payload.get("sign")
        self.logger.debug(f"Received sign: {msg_sign}")
        self.logger.debug(f"Command sign: {self.cmd.get('sign')}")
        if msg_sign != self.cmd.get("sign"):
            self.logger.error("Command signature does not match; ignoring result.")
            return

        success = payload.get("success", False)
        if not success:
            if self.curr_try < self.retries_num:
                self.curr_try += 1
                time.sleep(0.5)
                self.logger.debug(f"Retrying: attempt {self.curr_try}")
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

        lock_status = payload.get("lockStatus")
        if lock_status:
            self.parse_new_lockStatus(lock_status)

        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:
                pass

    # -------------------- Outgoing commands --------------------

    async def operateLock(self, lock_dir: int):
        """lock_dir: 1 = unlock ; 0 = lock"""
        self.logger.debug(f"operateLock called ({lock_dir})")
        self.curr_state = LOCK_STATE_OPERATING
        self.curr_try = 0
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:
                pass

        opCode = self._codes_generator.generateOperationCode(lock_dir, self.lockEvents)
        self.cmd = {
            "command1": "FF00" + opCode[0:36].decode("utf-8"),
            "command2": "FF01" + opCode[36:].decode("utf-8"),
            "sign": self._codes_generator.systemTime,
        }
        self.send_mqtt_command()

    def send_mqtt_command(self):
        mqtt.publish(
            self.hass,
            BLEOpTopic % self._config_topic,
            json.dumps(self.cmd),
        )

    # -------------------- Parse & normalize state --------------------

    def parse_new_lockStatus(self, lockStatus: str):
        self.logger.debug(f"Parsing new lockStatus: {lockStatus}")
        try:
            bArr = bytearray.fromhex(lockStatus)
        except Exception:
            self.logger.error(f"Bad lockStatus payload: {lockStatus}")
            return

        if len(bArr) < 20 or bArr[0] != 0xAA or bArr[3] != 0x02 or bArr[4] != 0x04:
            self.logger.error(f"Wrong lockStatus msg: {lockStatus}")
            return

        self.lockEvents = max(self.lockEvents, (bArr[10] << 24) | (bArr[11] << 16) | (bArr[12] << 8) | bArr[13])
        self.voltage = float(((bArr[14] << 8) | bArr[15]) * 0.01)

        raw_state = (bArr[16] >> 4) & 0x03
        self.curr_state = raw_state

        # Apply opensClockwise adjustment (keep JAMMED intact)
        if self.curr_state != LOCK_STATE_JAMMED and self.opensClockwise:
            self.curr_state = 1 - self.curr_state

        self.logger.debug(
            f"State trace (LOCKSTATUS): raw={raw_state} opensClockwise={self.opensClockwise} final={self.curr_state}"
        )
        self._publish_canonical_text_state()

    def parse_MQTT_advert(self, mqtt_advert: str):
        self.logger.debug(f"Parsing advert msg: {mqtt_advert}")
        try:
            bArr = bytearray.fromhex(mqtt_advert)
        except Exception:
            self.logger.error(f"Wrong advert msg: {mqtt_advert}")
            return

        if len(bArr) < 24 or bArr[0] != 0xBA or bArr[1] != 0xBA:
            self.logger.error(f"Wrong advert msg: {mqtt_advert}")
            return

        self.voltage = float(((bArr[16] << 8) | bArr[17]) * 0.01)
        self.boardModel = bArr[2]
        self.lversionOfSoft = bArr[3]
        self.sversionOfSoft = (bArr[4] << 16) | (bArr[5] << 8) | bArr[6]

        serialnumber = bArr[7:16].decode("utf-8", errors="ignore").strip("\0")
        if serialnumber and serialnumber != self._lockConfig.get("sn"):
            self.logger.error(
                f"ERROR: s/n in advert ({serialnumber}) is different from cloud data ({self._lockConfig.get('sn')})"
            )
        self.serialnumber = serialnumber or self._lockConfig.get("sn")

        events = (bArr[18] << 24) | (bArr[19] << 16) | (bArr[20] << 8) | bArr[21]
        self.lockEvents = max(self.lockEvents, events)

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

        self.curr_state = new_state
        if self.curr_state != LOCK_STATE_JAMMED and self.opensClockwise:
            self.curr_state = 1 - self.curr_state

        self.isBABA = True
        self.battery_perc = self.calculate_battery_percentage(self.voltage)

        self.logger.debug(
            f"State trace (ADV): raw={new_state} opensClockwise={self.opensClockwise} final={self.curr_state}"
        )

        # Canonical string state + other sensors
        self._publish_canonical_text_state()
        self._lockData[SENSOR_TYPE_BATTERY] = self.battery_perc
        self._lockData[SENSOR_TYPE_VOLTAGE] = self.voltage
        self._lockData[SENSOR_TYPE_BATTERY_LOW] = self.isLowBattery
        self._lockData[SENSOR_TYPE_LOCK_EVENTS] = self.lockEvents

    # -------------------- Canonical text state --------------------

    def _publish_canonical_text_state(self) -> None:
        """
        Publish canonical text state after all flips.
        If _state_invert=True, invert only 0/1 semantics (lock/unlock),
        keeping jammed/operating/failed untouched.
        """
        value = self.curr_state
        if self._state_invert and value in (LOCK_STATE_LOCKED, LOCK_STATE_UNLOCKED):
            value = 1 - value

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
            text_state = LOCK_STATE_STRINGS.get(value, "unknown")

        self._lockData[SENSOR_TYPE_STATE] = text_state

    # -------------------- Battery mapping --------------------

    def calculate_battery_percentage(self, voltage: float) -> float:
        """
        Piecewise map voltage to percentage using configured thresholds:
        voltages = [v0, v1, v2, v3] (historically); we use v0..v2.
        """
        voltages = self._lockConfig[CONF_VOLTAGE_THRESHOLDS]
        perc = 0.0
        if voltage >= voltages[2]:
            perc = 100.0
        elif voltage >= voltages[1]:
            perc = 66.6 + 33.3 * (voltage - voltages[1]) / (voltages[2] - voltages[1])
        else:
            perc = 33.3 + 33.3 * (voltage - voltages[0]) / (voltages[1] - voltages[0])
        perc = max(0.0, perc)
        return round(perc, 1)
