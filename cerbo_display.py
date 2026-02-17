#!/usr/bin/env python3
"""Victron Cerbo GX OLED Display Controller.

Reads solar/battery data via Modbus TCP from a Victron Cerbo GX ('einstein')
and displays it on an SH1106 OLED (I2C). Two toggle switches control
Generator and Multiplus II relays with 5s confirmation via pushbutton.

Modernized for pymodbus v3+, gpiozero, Python 3.10+.
"""

from __future__ import annotations

import datetime
import json
import logging
import signal
import socket
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gpiozero import Button
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import sh1106
from PIL import ImageFont
from pymodbus.client import ModbusTcpClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
FONT_PATH = str(SCRIPT_DIR / "fonts" / "PixelOperator-Bold.ttf")
CONFIG_PATH = SCRIPT_DIR / "config.json"
CONFIG_EXAMPLE_PATH = SCRIPT_DIR / "config.example.json"
HOME_DIR = Path.home()


def _load_config() -> dict:
    """Load config.json, creating from example if needed."""
    if not CONFIG_PATH.exists():
        if CONFIG_EXAMPLE_PATH.exists():
            shutil.copy(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
            log_early = logging.getLogger("cerbo_display")
            log_early.info("Created config.json from config.example.json")
        else:
            return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    """Save config dict back to config.json."""
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


_config = _load_config()

CERBO_HOST: str | None = _config.get("cerbo_host")
CERBO_PORT: int = _config.get("cerbo_port", 502)
CERBO_TIMEOUT: float = 5.0
CERBO_WATCHDOG_S: float = 60.0

I2C_PORT: int = _config.get("i2c_port", 1)
I2C_ADDRESS: int = int(str(_config.get("i2c_address", "0x3C")), 16) if isinstance(_config.get("i2c_address"), str) else _config.get("i2c_address", 0x3C)
DISPLAY_ROTATE: int = _config.get("display_rotate", 2)

# GPIO pins (BCM)
GPIO_GENERATOR: int = 17   # Kippschalter 1 → Generator (Relais 3)
GPIO_MULTIPLUS: int = 27   # Kippschalter 2 → Multiplus II (Relais 2)
GPIO_BUTTON: int = 24      # Taster → Screen-Wechsel

BUTTON_BOUNCE_S: float = 0.25
SCREEN_COUNT: int = 4
AUTO_RESET_TICKS: int = 10
SWITCH_CONFIRM_S: float = 5.0

# Modbus register map  (address, slave_id, divisor)
@dataclass(frozen=True)
class ModbusReg:
    """A single Modbus input-register definition."""
    address: int
    slave: int
    divisor: float = 1.0


# System registers (slave 100)
REG_PV_POWER    = ModbusReg(850, 100, 1)
REG_CHARGE_P    = ModbusReg(855, 100, 1000)
REG_DC_POWER    = ModbusReg(860, 100, 1000)
REG_SOC         = ModbusReg(843, 100, 1)
REG_BATT_POWER  = ModbusReg(842, 100, 1)
REG_BATT_STATE  = ModbusReg(844, 100, 1)
REG_BATT_V      = ModbusReg(840, 100, 10)
REG_AC1         = ModbusReg(817, 100, 1)
REG_AC2         = ModbusReg(818, 100, 1)
REG_AC3         = ModbusReg(819, 100, 1)

# Per-charger yield registers
REG_YIELD_1 = ModbusReg(784, 238, 10)
REG_YIELD_2 = ModbusReg(784, 239, 10)
REG_YIELD_3 = ModbusReg(784, 226, 10)
REG_YIELD_4 = ModbusReg(784, 224, 10)
REG_YIELD_5 = ModbusReg(784, 223, 10)
YIELD_REGS = [REG_YIELD_1, REG_YIELD_2, REG_YIELD_3, REG_YIELD_4, REG_YIELD_5]

# Battery health (BMS)
REG_BATT_HEALTH = ModbusReg(304, 225, 10)

# Relais read addresses (slave 100)
RELAIS_MULTIPLUS_ADDR = 807   # relaisnr 2 → addr 2+805
RELAIS_GENERATOR_ADDR = 3500  # relaisnr 3 → addr 3500

# Relais write addresses + slave
SWITCH_MAP: dict[int, tuple[int, int]] = {
    # schalter_id: (write_address, slave_id)
    2: (807, 100),   # Multiplus II
    3: (3500, 100),  # Generator
}

log = logging.getLogger("cerbo_display")

# ---------------------------------------------------------------------------
# Auto-Discovery
# ---------------------------------------------------------------------------

def _get_local_subnet() -> str | None:
    """Get local IP's /24 base (e.g. '192.168.1.')."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            timeout=5, text=True
        )
        for part in out.split():
            if part.count(".") == 3:
                pieces = part.split(".")
                try:
                    if all(0 <= int(p) <= 255 for p in pieces):
                        return ".".join(pieces[:3]) + "."
                except ValueError:
                    continue
    except Exception:
        pass
    # Fallback: connect UDP socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ".".join(ip.split(".")[:3]) + "."
    except Exception:
        return None


def _check_cerbo(ip: str, port: int = 502, timeout: float = 0.3) -> bool:
    """Quick check if ip:port has a Cerbo (Modbus slave 100, register 800)."""
    try:
        client = ModbusTcpClient(ip, port=port, timeout=timeout)
        if not client.connect():
            return False
        try:
            result = client.read_input_registers(800, count=1, device_id=100)
            return not result.isError()
        finally:
            client.close()
    except Exception:
        return False


def discover_cerbo(oled_device=None) -> str | None:
    """Scan local /24 network for Cerbo GX. Returns IP or None.
    Shows progress on OLED if device provided."""
    subnet = _get_local_subnet()
    if not subnet:
        log.warning("Could not determine local subnet")
        return None

    log.info("Scanning %s0/24 for Cerbo GX...", subnet)

    if oled_device:
        try:
            with canvas(oled_device) as draw:
                draw.rectangle(oled_device.bounding_box, outline="white")
                draw.text((5, 10), "Suche Cerbo...", font=ImageFont.truetype(FONT_PATH, 16), fill="white")
                draw.text((5, 35), subnet + "x", font=ImageFont.truetype(FONT_PATH, 16), fill="white")
        except Exception:
            pass

    # Try common addresses first
    priority = [1, 65, 100, 200, 2, 10, 50, 150, 254]
    tried = set()
    for last in priority:
        ip = subnet + str(last)
        tried.add(last)
        if _check_cerbo(ip):
            log.info("Cerbo found at %s (priority scan)", ip)
            return ip

    # Full scan
    for last in range(1, 255):
        if last in tried:
            continue
        ip = subnet + str(last)
        if _check_cerbo(ip):
            log.info("Cerbo found at %s (full scan)", ip)
            return ip

    log.warning("No Cerbo found on %s0/24", subnet)
    return None


# ---------------------------------------------------------------------------
# Fonts (lazy-loaded singleton)
# ---------------------------------------------------------------------------

@dataclass
class Fonts:
    """Font cache – loaded once."""
    _cache: dict[int, ImageFont.FreeTypeFont] = field(default_factory=dict, repr=False)

    def get(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self._cache:
            self._cache[size] = ImageFont.truetype(FONT_PATH, size)
        return self._cache[size]

    @property
    def f10(self) -> ImageFont.FreeTypeFont:
        return self.get(12)

    @property
    def f12(self) -> ImageFont.FreeTypeFont:
        return self.get(16)

    @property
    def f16(self) -> ImageFont.FreeTypeFont:
        return self.get(16)


fonts = Fonts()

# ---------------------------------------------------------------------------
# CerboModbus – persistent connection with auto-reconnect
# ---------------------------------------------------------------------------

class CerboModbus:
    """Persistent Modbus TCP connection to Victron Cerbo GX."""

    def __init__(self, host: str | None = CERBO_HOST, port: int = CERBO_PORT) -> None:
        if host is None:
            raise ValueError("No Cerbo host configured – run discovery first")
        self._host = host
        self._port = port
        self._client = ModbusTcpClient(host, port=port, timeout=CERBO_TIMEOUT)
        self._connected = False
        self._last_success: float = time.monotonic()
        self._cache: dict[tuple[int, int], float] = {}
        self._fail_count: dict[tuple[int, int], int] = {}
        self._blacklist: dict[tuple[int, int], float] = {}

    @property
    def seconds_since_success(self) -> float:
        return time.monotonic() - self._last_success

    @property
    def is_watchdog_expired(self) -> bool:
        return self.seconds_since_success > CERBO_WATCHDOG_S

    def _ensure_connected(self) -> bool:
        """Connect if not already connected. Returns True on success."""
        if self._connected and self._client.is_socket_open():
            return True
        try:
            self._connected = self._client.connect()
            if self._connected:
                log.info("Modbus connected to %s:%d", self._host, self._port)
            return self._connected
        except Exception:
            log.warning("Modbus connect failed", exc_info=True)
            self._connected = False
            return False

    def read_register(self, reg: ModbusReg) -> float | None:
        """Read a single input register. Returns scaled value or None on error.
        Blacklists registers that fail 3+ times (retries after 5 min)."""
        key = (reg.address, reg.slave)
        # Skip blacklisted registers (retry after 300s)
        bl_time = self._blacklist.get(key)
        if bl_time is not None:
            if time.monotonic() - bl_time < 300:
                return self._cached(reg)
            else:
                del self._blacklist[key]
                self._fail_count.pop(key, None)
                log.info("Retrying previously blacklisted reg %d slave %d",
                         reg.address, reg.slave)
        if not self._ensure_connected():
            return self._cached(reg)
        try:
            result = self._client.read_input_registers(
                reg.address, count=1, device_id=reg.slave
            )
            if result.isError():
                self._fail_count[key] = self._fail_count.get(key, 0) + 1
                if self._fail_count[key] >= 3:
                    log.warning("Blacklisting reg %d slave %d after %d failures "
                                "(retry in 5 min)",
                                reg.address, reg.slave, self._fail_count[key])
                    self._blacklist[key] = time.monotonic()
                else:
                    log.warning("Modbus read error reg %d slave %d (%d/3): %s",
                                reg.address, reg.slave, self._fail_count[key], result)
                return self._cached(reg)
            raw = result.registers[0]
            # Signed 16-bit conversion (original: if value > 60000)
            if raw > 60000:
                raw -= 65536
            value = raw / reg.divisor
            self._last_success = time.monotonic()
            self._cache[key] = value
            self._fail_count.pop(key, None)  # reset on success
            return value
        except Exception:
            log.warning("Modbus read exception reg %d slave %d",
                        reg.address, reg.slave, exc_info=True)
            self._mark_disconnected()
            return self._cached(reg)

    def read_relais(self, relais_addr: int, slave: int = 100) -> int | None:
        """Read a relay status register. Returns int value or None."""
        reg = ModbusReg(relais_addr, slave, 1)
        val = self.read_register(reg)
        if val is not None:
            return int(val)
        return None

    def write_register(self, address: int, slave: int, value: int) -> bool:
        """Write a single holding register."""
        if not self._ensure_connected():
            return False
        try:
            result = self._client.write_register(address, value, device_id=slave)
            if result.isError():
                log.error("Modbus write error addr %d slave %d: %s",
                          address, slave, result)
                return False
            self._last_success = time.monotonic()
            log.info("Modbus write addr=%d slave=%d val=%d OK", address, slave, value)
            return True
        except Exception:
            log.error("Modbus write exception", exc_info=True)
            self._mark_disconnected()
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        self._connected = False

    def _cached(self, reg: ModbusReg) -> float | None:
        """Return last known value for this register, or None."""
        return self._cache.get((reg.address, reg.slave))

    def _mark_disconnected(self) -> None:
        self._connected = False
        try:
            self._client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# OledDisplay – all rendering
# ---------------------------------------------------------------------------

class OledDisplay:
    """SH1106 OLED display rendering (128×64)."""

    def __init__(self, modbus: CerboModbus) -> None:
        serial = i2c(port=I2C_PORT, address=I2C_ADDRESS)
        self._device = sh1106(serial, rotate=DISPLAY_ROTATE)
        self._modbus = modbus
        self._last_frame: dict[str, Any] = {}

    @property
    def device(self) -> sh1106:
        return self._device

    # -- Helper ---------------------------------------------------------------

    @staticmethod
    def _now() -> tuple[str, str, int]:
        """Return (date_str, time_str, hour_int)."""
        now = datetime.datetime.now()
        return (
            now.strftime("%d.%m.%y"),
            now.strftime("%H:%M:%S"),
            now.hour,
        )

    def _read(self, reg: ModbusReg) -> float:
        """Read register, return 0 if None (display-safe)."""
        val = self._modbus.read_register(reg)
        return val if val is not None else 0

    def _read_relais_status(self, addr: int) -> int | None:
        return self._modbus.read_relais(addr)

    @staticmethod
    def _read_sun_file(path: str, chars: int = 2) -> str:
        """Read sunshine-hours file, return '?' on error."""
        try:
            return Path(path).read_text().strip()[:chars] if chars == 2 else Path(path).read_text().strip()
        except FileNotFoundError:
            log.warning("Sun file not found: %s", path)
            return "?"
        except Exception:
            log.warning("Error reading %s", path, exc_info=True)
            return "?"

    # -- Screens (pixel positions EXACTLY preserved) --------------------------

    def display_start(self) -> None:
        """Screen 0: Overview."""
        today_date, today_time, _ = self._now()

        mp_val = self._read_relais_status(RELAIS_MULTIPLUS_ADDR)
        match mp_val:
            case 1:   multiplus = "AN"
            case 0:   multiplus = "AUS"
            case _:    multiplus = "??"

        gen_val = self._read_relais_status(RELAIS_GENERATOR_ADDR)
        match gen_val:
            case 1:   genset = "AN"
            case 0:   genset = "AUS"
            case _:    genset = "??"

        pvpower = self._read(REG_PV_POWER)
        soc = format(self._read(REG_SOC), ".0f")
        battstate = self._read(REG_BATT_STATE)

        match int(battstate):
            case 1:
                battstatus = "Laden "
                battp: Any = self._read(REG_BATT_POWER)
            case 2:
                battstatus = "Entl. "
                battp = self._read(REG_BATT_POWER)
            case _:
                battstatus = "IDLE "
                battp = "0"

        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, outline="white")
            draw.text((5, 0), "PV: " + str(pvpower) + " W ", font=fonts.f12, fill="white")
            draw.text((5, 16), "SOC: " + str(soc) + " % ", font=fonts.f12, fill="white")
            draw.text((5, 32), battstatus + str(battp) + " W ", font=fonts.f12, fill="white")
            draw.text((5, 48), "MP: " + multiplus, font=fonts.f12, fill="white")
            draw.text((74, 48), "G: " + genset, font=fonts.f12, fill="white")

    def display_victron_pv(self) -> None:
        """Screen 1: PV Detail."""
        _, today_time, _ = self._now()

        pvpower = self._read(REG_PV_POWER)
        yieldtoday = sum(self._read(r) for r in YIELD_REGS)

        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, outline="white")
            draw.text((5, 0), "PV: " + str(pvpower) + " W ", font=fonts.f12, fill="white")
            draw.text((5, 20), "Ertrag: " + format(yieldtoday, ".1f") + " kWh", font=fonts.f12, fill="white")

    def display_batterie(self) -> None:
        """Screen 2: Battery."""
        _, today_time, _ = self._now()

        soc = format(self._read(REG_SOC), ".0f")
        battstate = self._read(REG_BATT_STATE)
        batthealth = self._read(REG_BATT_HEALTH)
        acpower = self._read(REG_AC1) + self._read(REG_AC2) + self._read(REG_AC3)

        match int(battstate):
            case 1:
                battstatus = "Laden"
                battp: Any = self._read(REG_BATT_POWER)
            case 2:
                battstatus = "Entl."
                battp = self._read(REG_BATT_POWER)
            case _:
                battstatus = "IDLE"
                battp = "0"

        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, outline="white")
            draw.text((5, 0), "SoC: " + str(soc) + " % ", font=fonts.f12, fill="white")
            draw.text((5, 16), str(battstatus) + ": " + str(battp) + " W ", font=fonts.f12, fill="white")
            draw.text((5, 32), "AC Last: " + str(acpower) + " W ", font=fonts.f12, fill="white")
            draw.text((5, 48), "SoH: " + str(batthealth) + " % ", font=fonts.f12, fill="white")

    def display_wetter(self) -> None:
        """Screen 3: Weather / sunshine hours."""
        today_date, _, hour = self._now()

        sun_heute_path = str(HOME_DIR / ".sonneheute")
        sun_morgen_path = str(HOME_DIR / ".sonnemorgen")
        sun_ueberm_path = str(HOME_DIR / ".sonneuebermorgen")
        datum_path = str(HOME_DIR / ".datum")

        if hour > 20:
            sonneheute = "schon weg"
            sonnemorgen = self._read_sun_file(sun_heute_path)
            sonneuebermorgen = self._read_sun_file(sun_ueberm_path)
        else:
            sonneheute = self._read_sun_file(sun_heute_path)
            sonnemorgen = self._read_sun_file(sun_morgen_path)
            sonneuebermorgen = self._read_sun_file(sun_ueberm_path)

        stamp = self._read_sun_file(datum_path, chars=99)

        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, outline="white")
            draw.text((5, 0), str(stamp.strip()) + " Uhr", font=fonts.f12, fill="white")
            draw.text((5, 16), "heute: " + format(float(sonneheute.strip() or 0), ".1f") + " h", font=fonts.f12, fill="white")
            draw.text((5, 32), "morgen: " + format(float(sonnemorgen.strip() or 0), ".1f") + " h", font=fonts.f12, fill="white")
            draw.text((5, 48), "überm: " + format(float(sonneuebermorgen.strip() or 0), ".1f") + " h", font=fonts.f12, fill="white")

    def display_lan_error(self) -> None:
        """Show LAN error screen."""
        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, outline="white")
            draw.text((5, 5), "LAN-Kabel ", font=fonts.f12, fill="white")
            draw.text((5, 19), "einstecken oder ", font=fonts.f12, fill="white")
            draw.text((5, 31), "CERBO mit ", font=fonts.f12, fill="white")
            draw.text((5, 41), "LAN verbinden! ", font=fonts.f12, fill="white")

    def display_schalter_umschalten(self, relaisnr: int, status: int) -> None:
        """Show switch confirmation prompt."""
        today_date, today_time, _ = self._now()

        match relaisnr:
            case 1:  schalter = "SCHALTER 1:"
            case 2:  schalter = "Multiplus II:"
            case 3:  schalter = "Generator:"
            case _:  schalter = "?"

        zustand = " AN" if status == 1 else " AUS"

        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, outline="white")
            draw.text((75, 3), today_time, font=fonts.f12, fill="yellow")
            draw.text((5, 12), "UMSCHALTEN:", font=fonts.f12, fill="yellow")
            draw.text((5, 28), schalter + zustand, font=fonts.f12, fill="white")
            draw.text((5, 44), "..TASTER..in 5 Sek.!", font=fonts.f12, fill="white")

    def display_schalter_start(self, relaisnr: int) -> None:
        """Show switch initialization screen."""
        today_date, today_time, _ = self._now()

        match relaisnr:
            case 1:  schalter = "SCHALTER 1"
            case 2:  schalter = "Multiplus II"
            case 3:  schalter = "Generator"
            case _:  schalter = "?"

        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, outline="white")
            draw.text((75, 3), today_time, font=fonts.f12, fill="yellow")
            draw.text((2, 12), "INITIALISIERUNG", font=fonts.f12, fill="yellow")
            draw.text((2, 28), schalter, font=fonts.f12, fill="white")
            draw.text((2, 44), "bitte umschalten", font=fonts.f12, fill="white")

    def display_schalter_abgebrochen(self) -> None:
        """Show 'switch cancelled' screen for 2 seconds."""
        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, outline="white")
            draw.text((5, 5), "Vorgang ", font=fonts.f12, fill="white")
            draw.text((5, 19), "abgebrochen, ", font=fonts.f12, fill="white")
            draw.text((5, 31), "nicht ", font=fonts.f12, fill="white")
            draw.text((5, 41), "geschaltet! ", font=fonts.f12, fill="white")
        time.sleep(2)

    def show_screen(self, index: int) -> None:
        """Render screen by index (0-3)."""
        if self._modbus.is_watchdog_expired:
            self.display_lan_error()
            return
        match index:
            case 0: self.display_start()
            case 1: self.display_victron_pv()
            case 2: self.display_batterie()
            case 3: self.display_wetter()


# ---------------------------------------------------------------------------
# SwitchController – GPIO toggle switches + button
# ---------------------------------------------------------------------------

class SwitchController:
    """Manages toggle switches and the screen-cycle pushbutton via gpiozero."""

    def __init__(self, modbus: CerboModbus, display: OledDisplay) -> None:
        self._modbus = modbus
        self._display = display
        self._counter: int = 0
        self._tick: int = 0

        # gpiozero Buttons (pull_up=True is default, active_state matches original logic)
        self._sw_generator = Button(GPIO_GENERATOR, pull_up=True, bounce_time=None)
        self._sw_multiplus = Button(GPIO_MULTIPLUS, pull_up=True, bounce_time=None)
        self._btn = Button(GPIO_BUTTON, pull_up=True, bounce_time=BUTTON_BOUNCE_S)

        self._btn.when_pressed = self._on_button_press

    @property
    def counter(self) -> int:
        return self._counter

    @counter.setter
    def counter(self, value: int) -> None:
        self._counter = value

    @property
    def tick(self) -> int:
        return self._tick

    @tick.setter
    def tick(self, value: int) -> None:
        self._tick = value

    def _on_button_press(self) -> None:
        """ISR: cycle through screens."""
        self._counter += 1
        if self._counter > 3:
            self._counter = 0
        log.debug("Button → counter=%d", self._counter)
        self._tick = 0

    def read_switch(self, gpio: int) -> int:
        """Read physical switch state (1=pressed/high, 0=low)."""
        match gpio:
            case _ if gpio == GPIO_GENERATOR:
                return int(self._sw_generator.is_pressed)
            case _ if gpio == GPIO_MULTIPLUS:
                return int(self._sw_multiplus.is_pressed)
            case _:
                return 0

    def _relais_addr_for(self, schalter_id: int) -> int:
        """Map schalter id to relay read address."""
        if schalter_id == 3:
            return RELAIS_GENERATOR_ADDR
        return schalter_id + 805

    def _gpio_for(self, schalter_id: int) -> int:
        if schalter_id == 3:
            return GPIO_GENERATOR
        if schalter_id == 2:
            return GPIO_MULTIPLUS
        return GPIO_GENERATOR  # schalter 1 mapped to SCHALTER1 in original

    def check_switch(self, schalter_id: int) -> None:
        """Check if a toggle switch changed vs. Cerbo relay state, do 5s confirm."""
        gpio = self._gpio_for(schalter_id)
        write_addr, write_slave = SWITCH_MAP.get(schalter_id, (0, 100))
        relais_addr = self._relais_addr_for(schalter_id)

        current_switch = self.read_switch(gpio)
        relay_state = self._modbus.read_relais(relais_addr)

        if relay_state is None:
            return  # can't compare, skip

        if current_switch == relay_state:
            return  # in sync

        log.info("Switch %d toggled – waiting 5s for confirmation", schalter_id)
        desired = current_switch
        self._display.display_schalter_umschalten(schalter_id, desired)
        counter_before = self._counter
        time.sleep(SWITCH_CONFIRM_S)

        # Verify switch still in same position AND button was pressed during wait
        if self.read_switch(gpio) == desired and self._counter > counter_before:
            self._modbus.write_register(write_addr, write_slave, desired)
            self._counter = 0
        else:
            self._display.display_schalter_abgebrochen()
            log.info("Switch %d cancelled", schalter_id)

    def wait_for_sync(self, schalter_id: int) -> None:
        """Block until physical switch matches relay (startup sync).
        Skips after 10 failed attempts (register may not exist on this Cerbo)."""
        gpio = self._gpio_for(schalter_id)
        relais_addr = self._relais_addr_for(schalter_id)
        fail_count = 0

        while True:
            relay = self._modbus.read_relais(relais_addr)
            if relay is None:
                fail_count += 1
                if fail_count >= 10:
                    log.warning("Skipping sync for switch %d – register %d "
                                "unavailable after %d attempts",
                                schalter_id, relais_addr, fail_count)
                    break
                self._display.display_lan_error()
                time.sleep(0.5)
                continue
            if self.read_switch(gpio) == relay:
                break
            log.info("Switch %d out of sync with relay, waiting...", schalter_id)
            self._display.display_schalter_start(schalter_id)
            time.sleep(0.5)

    def close(self) -> None:
        """Release gpiozero resources."""
        self._sw_generator.close()
        self._sw_multiplus.close()
        self._btn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point – initialize and run main loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # --- Config / Auto-Discovery ---
    global CERBO_HOST
    cerbo_host = CERBO_HOST

    if not cerbo_host:
        # Init OLED early for discovery status display
        try:
            serial_early = i2c(port=I2C_PORT, address=I2C_ADDRESS)
            oled_early = sh1106(serial_early, rotate=DISPLAY_ROTATE)
        except Exception:
            oled_early = None

        cerbo_host = discover_cerbo(oled_device=oled_early)
        if cerbo_host:
            _config["cerbo_host"] = cerbo_host
            _save_config(_config)
            CERBO_HOST = cerbo_host
            log.info("Cerbo discovered at %s, saved to config.json", cerbo_host)
            # Close early OLED so OledDisplay can re-init
            if oled_early:
                try:
                    oled_early.cleanup()
                except Exception:
                    pass
        else:
            log.error("No Cerbo GX found on network. Set cerbo_host in config.json manually.")
            if oled_early:
                try:
                    with canvas(oled_early) as draw:
                        draw.rectangle(oled_early.bounding_box, outline="white")
                        draw.text((5, 10), "Kein Cerbo", font=ImageFont.truetype(FONT_PATH, 16), fill="white")
                        draw.text((5, 30), "gefunden!", font=ImageFont.truetype(FONT_PATH, 16), fill="white")
                except Exception:
                    pass
            time.sleep(5)
            sys.exit(1)

    modbus = CerboModbus(host=cerbo_host)
    display = OledDisplay(modbus)
    switches = SwitchController(modbus, display)
    running = True

    def _shutdown(signum: int, _frame: Any) -> None:
        nonlocal running
        log.info("Signal %d received, shutting down...", signum)
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Startup: wait for switches to match relay state
    log.info("Startup sync – Generator")
    switches.wait_for_sync(3)
    log.info("Startup sync – Multiplus II")
    switches.wait_for_sync(2)

    sw1_state = switches.read_switch(GPIO_GENERATOR)
    sw2_state = switches.read_switch(GPIO_MULTIPLUS)
    log.info("Generator switch: %s", "AN" if sw1_state else "AUS")
    log.info("Multiplus switch: %s", "AN" if sw2_state else "AUS")

    # Main loop
    while running:
        switches.check_switch(3)  # Generator
        switches.check_switch(2)  # Multiplus II

        switches.tick += 1
        time.sleep(1)

        display.show_screen(switches.counter)

        if switches.tick > 9:
            switches.counter = 0
            switches.tick = 0

    # Cleanup
    log.info("Cleaning up...")
    switches.close()
    modbus.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
