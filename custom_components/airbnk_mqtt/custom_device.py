from __future__ import annotations

import json
import time
from typing import Callable

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from .airbnk_logger import AirbnkLogger
from .codes_generator import AirbnkCodesGenerator
from .const import (
    DOMAIN as AIRBNK_DOMAIN,
    SENSOR_TYPE_BATTERY,
    SENSOR_TYPE_BATTERY_LOW,
    SENSOR_TYPE_LAST_ADVERT,
    SENSOR_TYPE_LOCK_EVENTS,
    SENSOR_TYPE_SIGNAL_STRENGTH,
    SENSOR_TYPE_STATE,
    SENSOR_TYPE_VOLTAGE,
    CONF_MAC_ADDRESS,
    CONF_MQTT_TOPIC,
    CONF_RETRIES_NUM,
    CONF_VOLTAGE_THRESHOLDS,
    DEFAULT_RETRIES_NUM,
    LOCK_STATE_FAILED,
    LOCK_STATE_JAMMED,
    LOCK_STATE_LOCKED,
    LOCK_STATE_OPERATING,
    LOCK_STATE_STRINGS,
    LOCK_STATE_UNLOCKED,
)

MAX_NORECEIVE_TIME = 30

BLETelemetryTopic = "%s/tele"
BLEOpTopic = "%s/command"
BLEStateTopic = "%s/adv"
BLEOperationReportTopic = "%s/command_result"


class CustomMqttLockDevice:
    utcMinutes = None
    voltage = None
    isBackLock = None
    isInit = None
    isImageA = None
    isHadNewRecord = None
    curr_state = LOCK_STATE_UNLOCKED
    softVersion = None
    isEnableAuto = None
    opensClockwise = None
    isLowBattery = None
    magnetcurr_state = None
    isMagnetEnable = None
    isBABA = None
    lversionOfSoft = None
    versionOfSoft = None
    versionCode = None
    serialnumber = None
    lockEvents = 0

    _lockConfig = {}
    _lockData = {}
    _codes_generator = None
    cmd = {}

    last_advert_time = 0
    last_telemetry_time = 0
    is_available = False

    retries_num = DEFAULT_RETRIES_NUM
    curr_try = 0

    def __init__(self, hass: HomeAssistant, device_config, entry_options):
        self.logger = AirbnkLogger(__name__)
        self.logger.debug("Setting up CustomMqttLockDevice for sn %s" % device_config["sn"])
        self.hass = hass
        self._callbacks = set()
        self._unsubscribe_callbacks = set()
        self._lockConfig = device_config
        self._codes_generator = AirbnkCodesGenerator()
        self._lockData = self._codes_generator.decryptKeys(
            device_config["newSninfo"], device_config["appKey"]
        )
        self.set_options(entry_options)
        self.logger.debug("...done")

    @property
    def device_info(self):
        """Return a device description for device registry."""
        devID = self._lockData["lockSn"]
        return {
            "identifiers": {(AIRBNK_DOMAIN, devID)},
            "manufacturer": "Airbnk",
            "model": self._lockConfig["deviceType"],
            "name": self._lockConfig["deviceName"],
            "sw_version": self._lockConfig["firmwareVersion"],
            "connections": {(CONNECTION_NETWORK_MAC, self._lockConfig[CONF_MAC_ADDRESS])},
        }

    def check_availability(self):
        curr_time = int(round(time.time()))
        deltatime1 = curr_time - self.last_advert_time
        deltatime2 = curr_time - self.last_telemetry_time
        if min(deltatime1, deltatime2) >= MAX_NORECEIVE_TIME:
            self.is_available = False

    @property
    def islocked(self) -> bool | None:
        return self.curr_state == LOCK_STATE_LOCKED

    @property
    def isunlocked(self) -> bool | None:
        return self.curr_state == LOCK_STATE_UNLOCKED

    @property
    def isjammed(self) -> bool | None:
        return self.curr_state == LOCK_STATE_JAMMED

    @property
    def state(self):
        return LOCK_STATE_STRINGS[self.curr_state]

    def set_options(self, entry_options):
        self.logger.debug("Options set: %s" % entry_options)
        self.retries_num = entry_options.get(CONF_RETRIES_NUM, DEFAULT_RETRIES_NUM)

    async def mqtt_subscribe(self):
        if "mqtt" not in self.hass.data:
            self.logger.error(
                "MQTT is not connected: cannot subscribe. Have you configured an MQTT Broker?"
            )
            return

        @callback
        async def adv_received(_p0) -> None:
            self.parse_adv_message(_p0.payload)

        @callback
        async def operation_msg_received(_p0) -> None:
            self.parse_operation_message(_p0.payload)

        @callback
        async def telemetry_msg_received(_p0) -> None:
            self.parse_telemetry_message(_p0.payload)

        callback_func = await mqtt.async_subscribe(
            self.hass,
            BLEStateTopic % self._lockConfig[CONF_MQTT_TOPIC],
            msg_callback=adv_received,
        )
        self._unsubscribe_callbacks.add(callback_func)

        callback_func = await mqtt.async_subscribe(
            self.hass,
            BLETelemetryTopic % self._lockConfig[CONF_MQTT_TOPIC],
            msg_callback=telemetry_msg_received,
        )
        self._unsubscribe_callbacks.add(callback_func)

        callback_func = await mqtt.async_subscribe(
            self.hass,
            BLEOperationReportTopic % self._lockConfig[CONF_MQTT_TOPIC],
            msg_callback=operation_msg_received,
        )
        self._unsubscribe_callbacks.add(callback_func)

    async def mqtt_unsubscribe(self):
        for callback_func in self._unsubscribe_callbacks:
            callback_func()

    def register_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.add(callback)

    def parse_telemetry_message(self, msg):
        self.logger.debug("Received telemetry %s" % msg)
        self.last_telemetry_time = int(round(time.time()))
        self.is_available = True

    def parse_adv_message(self, msg):
        self.logger.debug("Received adv %s" % msg)
        payload = json.loads(msg)
        mac_address = self._lockConfig[CONF_MAC_ADDRESS]
        mqtt_advert = payload["data"]
        mqtt_mac = payload["mac"].replace(":", "").upper()
        self.logger.debug("Config mac %s, received %s" % (mac_address, mqtt_mac))
        if mqtt_mac != mac_address.upper():
            return

        self.parse_MQTT_advert(mqtt_advert.upper())
        time2 = self.last_advert_time
        self.last_advert_time = int(round(time.time()))

        if "rssi" in payload:
            self._lockData[SENSOR_TYPE_SIGNAL_STRENGTH] = payload["rssi"]

        deltatime = self.last_advert_time - time2
        self._lockData[SENSOR_TYPE_LAST_ADVERT] = deltatime
        self.is_available = True
        self.logger.debug("Time from last message: %s secs" % str(deltatime))

        for callback_func in self._callbacks:
            callback_func()

    def parse_operation_message(self, msg):
        self.logger.debug("Received operation result %s" % msg)
        payload = json.loads(msg)
        mac_address = self._lockConfig[CONF_MAC_ADDRESS]
        mqtt_mac = payload["mac"].replace(":", "").upper()

        if mqtt_mac != mac_address.upper():
            return

        msg_sign = payload["sign"]
        self.logger.debug("Received sign: %s" % msg_sign)
        self.logger.debug("Command sign: %s" % self.cmd["sign"])
        if msg_sign != self.cmd["sign"]:
            self.logger.error("Returning.")
            return

        msg_state = payload["success"]
        if msg_state is False:
            if self.curr_try < self.retries_num:
                self.curr_try += 1
                time.sleep(0.5)
                self.logger.debug("Retrying: attempt %i" % self.curr_try)
                self.curr_state = LOCK_STATE_OPERATING
                for callback_func in self._callbacks:
                    callback_func()
                self.send_mqtt_command()
            else:
                self.logger.error("No more retries: command FAILED")
                self.curr_state = LOCK_STATE_FAILED
                for callback_func in self._callbacks:
                    callback_func()
                raise Exception("Failed sending command: returned %s", msg_state)
            return
        else:
            self.parse_new_lockStatus(payload["lockStatus"])
            for callback_func in self._callbacks:
                callback_func()

    async def operateLock(self, lock_dir):
        self.logger.debug("operateLock called (%s)" % lock_dir)
        self.curr_state = LOCK_STATE_OPERATING
        self.curr_try = 0
        for callback_func in self._callbacks:
            callback_func()

        opCode = self._codes_generator.generateOperationCode(lock_dir, self.lockEvents)
        self.cmd = {}
        self.cmd["command1"] = "FF00" + opCode[0:36].decode("utf-8")
        self.cmd["command2"] = "FF01" + opCode[36:].decode("utf-8")
        self.cmd["sign"] = self._codes_generator.systemTime
        self.send_mqtt_command()

    def send_mqtt_command(self):
        mqtt.publish(
            self.hass,
            BLEOpTopic % self._lockConfig[CONF_MQTT_TOPIC],
            json.dumps(self.cmd),
        )

    # ---- Voltage decoding helpers ----
    def _decode_tail_voltage_mv(self, bArr: bytearray) -> float | None:
        """
        New/alternate advert format (your lock):
          tail ends with 00 00 00 and the 16-bit value before that is millivolts (little-endian).
        Example tails:
          ... 8212 000000 -> 0x1282=4738 -> 4.738V
          ... BE12 000000 -> 0x12BE=4798 -> 4.798V
        """
        if len(bArr) < 7:
            return None
        if not (bArr[-1] == 0x00 and bArr[-2] == 0x00 and bArr[-3] == 0x00):
            return None

        raw_mv = (bArr[-4] << 8) | bArr[-5]  # little-endian
        # sanity: pack voltage range (adjust if you ever see values outside this)
        if 3000 <= raw_mv <= 7000:
            return raw_mv / 1000.0
        return None

    def parse_new_lockStatus(self, lockStatus):
        self.logger.debug("Parsing new lockStatus: %s" % lockStatus)
        bArr = bytearray.fromhex(lockStatus)
        if bArr[0] != 0xAA or bArr[3] != 0x02 or bArr[4] != 0x04:
            self.logger.error("Wrong lockStatus msg: %s" % lockStatus)
            return

        lockEvents = (bArr[10] << 24) | (bArr[11] << 16) | (bArr[12] << 8) | bArr[13]
        self.lockEvents = max(self.lockEvents, lockEvents)

        # Upstream lockStatus voltage (centivolts) uses bArr[14:16] * 0.01 
        legacy_voltage = ((float)((bArr[14] << 8) | bArr[15])) * 0.01

        # Prefer tail mV format if present (some models/firmwares differ) [1](https://github.com/Koenkk/zigbee2mqtt/blob/master/data/configuration.example.yaml)
        tail_voltage = self._decode_tail_voltage_mv(bArr)
        self.voltage = tail_voltage if tail_voltage is not None else legacy_voltage

        self.curr_state = (bArr[16] >> 4) & 3

    def parse_MQTT_advert(self, mqtt_advert):
        self.logger.debug("Parsing advert msg: %s" % mqtt_advert)
        bArr = bytearray.fromhex(mqtt_advert)
        if bArr[0] != 0xBA or bArr[1] != 0xBA:
            self.logger.error("Wrong advert msg: %s" % mqtt_advert)
            return

        # Upstream advert voltage assumes bArr[16:18] * 0.01 
        legacy_voltage = ((float)((bArr[16] << 8) | bArr[17])) * 0.01

        # Prefer tail mV format for your lock (alternate format) [1](https://github.com/Koenkk/zigbee2mqtt/blob/master/data/configuration.example.yaml)
        tail_voltage = self._decode_tail_voltage_mv(bArr)
        self.voltage = tail_voltage if tail_voltage is not None else legacy_voltage

        self.boardModel = bArr[2]
        self.lversionOfSoft = bArr[3]
        self.sversionOfSoft = (bArr[4] << 16) | (bArr[5] << 8) | bArr[6]

        serialnumber = bArr[7:16].decode("utf-8").strip("\0")
        if serialnumber != self._lockConfig["sn"]:
            self.logger.error(
                "ERROR: s/n in advert (%s) is different from cloud data (%s)"
                % (serialnumber, self._lockConfig["sn"])
            )

        lockEvents = (bArr[18] << 24) | (bArr[19] << 16) | (bArr[20] << 8) | bArr[21]
        new_state = (bArr[22] >> 4) & 3
        self.opensClockwise = (bArr[22] & 0x80) != 0
        self.lockEvents = max(self.lockEvents, lockEvents)

        if self.curr_state != LOCK_STATE_FAILED:
            self.curr_state = new_state
            if self.opensClockwise and self.curr_state != LOCK_STATE_JAMMED:
                self.curr_state = 1 - self.curr_state

        z = False
        self.isBackLock = (bArr[22] & 1) != 0
        self.isInit = (2 & bArr[22]) != 0
        self.isImageA = (bArr[22] & 4) != 0
        self.isHadNewRecord = (bArr[22] & 8) != 0
        self.isEnableAuto = (bArr[22] & 0x40) != 0
        self.isLowBattery = (bArr[23] & 0x10) != 0
        self.magnetcurr_state = (bArr[23] >> 5) & 3
        if (bArr[23] & 0x80) != 0:
            z = True

        self.isMagnetEnable = z
        self.isBABA = True

        self.battery_perc = self.calculate_battery_percentage(self.voltage)
        self._lockData[SENSOR_TYPE_STATE] = self.state
        self._lockData[SENSOR_TYPE_BATTERY] = self.battery_perc
        self._lockData[SENSOR_TYPE_VOLTAGE] = self.voltage
        self._lockData[SENSOR_TYPE_BATTERY_LOW] = self.isLowBattery
        self._lockData[SENSOR_TYPE_LOCK_EVENTS] = self.lockEvents

        return

    def calculate_battery_percentage(self, voltage):
        voltages = self._lockConfig[CONF_VOLTAGE_THRESHOLDS]
        perc = 0
        if voltage >= voltages[2]:
            perc = 100
        elif voltage >= voltages[1]:
            perc = 66.6 + 33.3 * (voltage - voltages[1]) / (voltages[2] - voltages[1])
        else:
            perc = 33.3 + 33.3 * (voltage - voltages[0]) / (voltages[1] - voltages[0])
        perc = max(perc, 0)
        return round(perc, 1)
