#!/usr/bin/env python3
"""
Nibe-Huawei Bridge – Home Assistant Addon
==========================================
Emuluje Huawei SUN2000 inverter cez Modbus TCP.
Nibe S1255 sa pripojí priamo ako Modbus master a číta PV/batériové/sieťové dáta.

Dva režimy (môžu bežať súbežne):
  1. modbus_server – embedded Modbus TCP slave server mimiking SUN2000
     (Nibe číta registre priamo, bez HA Modbus integrácie)
  2. surplus_control – vypočítava prebytok a priamo nastavuje HW comfort mode
     a heating offset cez HA modbus.write_register (fallback)
"""

import asyncio
import aiohttp
import json
import logging
import os
import struct
import sys
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

log = logging.getLogger("nibe-huawei")

# ---------------------------------------------------------------------------
# Register constants
# ---------------------------------------------------------------------------

# Huawei SUN2000MA (MAP0) Modbus registers — MBSA V3.0 spec, model SUN2000_10K_MAP0
HUAWEI_REG_DC_INPUT   = 32064  # INT32, 2 regs, kW×1000 = W (total DC input from PV)  [MAP0 spec §3.1 #135]
HUAWEI_REG_ACTIVE_PWR = 32080  # INT32, 2 regs, kW×1000 = W (AC output active power)  [MAP0 spec §3.1 #146]
HUAWEI_REG_GRID_POWER = 37113  # INT32, 2 regs, W (+export / -import)                  [MAP0 spec §3.1 #267]
HUAWEI_REG_BATT_SOC   = 37760  # UINT16, 1 reg, % × 10 (combined ESU SOC)              [MAP0 spec §3.2 #33]
HUAWEI_REG_BATT_POWER = 37765  # INT32, 2 regs, W (+charge / -discharge, combined ESU) [MAP0 spec §3.2 #37]

# SUN2000 device info registers (older Modbus spec — used by Nibe S1255)
NIBE_REG_PV_POWER     = 30071  # UINT16, 1 reg, W — PV power (old spec; V3.0 redefines as #PV strings, but Nibe reads this as produced power)
NIBE_REG_RATED_POWER  = 30073  # UINT16, 1 reg, kW — rated power of inverter
NIBE_REG_BATT_MAX_CHG = 37046  # UINT32, 2 regs, W — max charge power  [MAP0 spec §3.2 #16]
NIBE_REG_BATT_MAX_DIS = 37048  # UINT32, 2 regs, W — max discharge power [MAP0 spec §3.2 #17]

# Battery unit 1 registers [MAP0 spec §3.2]
HUAWEI_REG_STORAGE_STATUS = 37000  # UINT16, 1 reg — 0=offline,1=standby,2=running,3=fault,4=sleep
HUAWEI_REG_STORAGE_POWER  = 37001  # INT32, 2 regs, W — unit 1 charge/discharge power (+charge/-discharge)
HUAWEI_REG_STORAGE_SOC    = 37004  # UINT16, 1 reg, % × 10 — unit 1 SOC
HUAWEI_REG_ESU_STATUS     = 37762  # UINT16, 1 reg — ESU running status: 0=offline,1=standby,2=running [MAP0 §3.2 #34]

# Smart meter status [MAP0 spec §3.1]
HUAWEI_REG_METER_STATUS   = 37100  # UINT16, 1 reg — 0=offline, 1=normal

# PV string DC inputs [MAP0 spec §3.1]
HUAWEI_REG_PV1_VOLTAGE = 32016  # UINT16, gain 10, V — PV string 1 DC voltage
HUAWEI_REG_PV2_VOLTAGE = 32018  # UINT16, gain 10, V — PV string 2 DC voltage

# Inverter output registers [MAP0 spec §3.1]
HUAWEI_REG_TEMPERATURE  = 32087  # INT16, gain 10, °C
HUAWEI_REG_POWER_FACTOR = 32084  # INT16, gain 1000
HUAWEI_REG_GRID_FREQ    = 32085  # UINT16, gain 100, Hz

# Energy counters [MAP0 spec §3.1]
HUAWEI_REG_TOTAL_YIELD = 32106  # UINT32, gain 100, kWh (lifetime)
HUAWEI_REG_DAILY_YIELD = 32114  # UINT32, gain 100, kWh (today)

# Smart meter energy counters [MAP0 spec §3.1 #271 / #272]
HUAWEI_REG_GRID_EXPORT = 37119  # INT32, gain 100, kWh (positive active energy = exported)
HUAWEI_REG_GRID_IMPORT = 37121  # INT32, gain 100, kWh (reverse active energy = imported)

# SunSpec magic – populated so Nibe finds either proprietary or SunSpec regs
SUNSPEC_BASE          = 40000  # "SunS" identifier (2 regs)
SUNSPEC_MODEL1_BASE   = 40002  # Model 1 (Common), length 66
SUNSPEC_MODEL103_BASE = 40070  # Model 103 (Three-phase inverter), length 50
SUNSPEC_M103_W        = 40084  # Model 103 AC Power (INT16, offset 14 from base 40070, W with W_SF)
SUNSPEC_M103_W_SF     = 40085  # Model 103 W_SF scale factor (INT16, exponent; 0 = ×1 = watts)

# Nibe registers used by surplus_control (unchanged)
REG_HW_COMFORT_MODE = 47041   # 0=ECO, 1=Normal, 2=Luxury
REG_HEATING_OFFSET  = 47276   # Heating offset climate system (-10 to +10)

# ---------------------------------------------------------------------------
# Konfigurácia
# ---------------------------------------------------------------------------

OPTIONS_FILE = "/data/options.json"
HA_BASE_URL  = "http://supervisor/core"


def load_options() -> dict:
    try:
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("options.json nenájdené, používam defaulty")
        return {}


# ---------------------------------------------------------------------------
# Modbus register helpers
# ---------------------------------------------------------------------------

def _pack_int32(value: int) -> list[int]:
    """Pack signed int32 into two big-endian 16-bit Modbus words."""
    clamped = max(-(2**31), min(2**31 - 1, value))
    raw = struct.pack(">i", clamped)
    return [(raw[0] << 8) | raw[1], (raw[2] << 8) | raw[3]]


def _pack_uint16(value: int) -> list[int]:
    """Pack unsigned int into one 16-bit Modbus word."""
    return [max(0, min(0xFFFF, value))]


# ---------------------------------------------------------------------------
# RegisterBank – plain dict + helpers for SUN2000 register layout
# ---------------------------------------------------------------------------

def _str_to_regs(s: str, num_regs: int) -> list[int]:
    """Encode ASCII string into Modbus registers (2 chars per register, big-endian)."""
    padded = s.ljust(num_regs * 2, "\x00")[:num_regs * 2]
    return [(ord(padded[i]) << 8) | ord(padded[i + 1]) for i in range(0, num_regs * 2, 2)]


def build_register_dict(rated_power_kw: int = 10) -> dict:
    """
    Build a plain dict[int, int] covering all known SUN2000 and SunSpec register
    addresses.  Missing addresses return 0 at read time (no IllegalAddress).
    """
    regs: dict[int, int] = {}

    # Device identification — exact MAP0 spec layout (mirrors real SUN2000-10K-MAP0 inverter)
    for i, val in enumerate(_str_to_regs("SUN2000-10K-MAP0", 15)):
        regs[30000 + i] = val                          # 30000-30014: Model name (STRING30, 15 regs)
    for i, val in enumerate(_str_to_regs("BT2470706907", 10)):
        regs[30015 + i] = val                          # 30015-30024: Serial number (STRING20, 10 regs)
    for addr in range(30025, 30035):
        regs[addr] = 0                                 # 30025-30034: reserved
    for i, val in enumerate(_str_to_regs("V200R024D02", 15)):
        regs[30035 + i] = val                          # 30035-30049: Firmware version (STRING30, 15 regs)
    for i, val in enumerate(_str_to_regs("V200R024C00SPC108", 15)):
        regs[30050 + i] = val                          # 30050-30064: Software version (STRING30, 15 regs)
    for addr in range(30065, 30071):
        regs[addr] = 0                                 # 30065-30069: reserved/status
    regs[30070] = 1004                                 # MAP0 model ID
    regs[30071] = 0                                    # PV power W (old Modbus spec; Nibe reads as "Produced power") — updated live
    regs[30073] = rated_power_kw                       # Rated power in kW (UINT16, gain 1)

    # SUN2000 inverter data registers
    regs[32000] = 6                                    # Device state: grid-connected + battery active
    for addr in range(32008, 32011):
        regs[addr] = 0
    for addr in range(32016, 32200):                   # DC strings + AC outputs + misc
        regs[addr] = 0
    regs[32064] = 0;  regs[32065] = 0                 # Total DC input from PV (INT32, W) — updated live
    regs[32080] = 0;  regs[32081] = 0                 # AC active power output (INT32, W) — updated live
    regs[32084] = 1000                                 # Power factor (INT16, gain 1000 → 1.000)
    regs[32085] = 5000                                 # Grid frequency (UINT16, Hz×100 → 50.00 Hz)
    regs[32089] = 512                                  # Running state: grid-connected/running

    # Smart meter data registers [MAP0 spec §3.1 #261-281]
    regs[37100] = 1                                    # Meter status: 1=normal
    regs[37101] = 0;   regs[37102] = 2300             # Phase A grid voltage (INT32, V×10 → 230.0V)
    regs[37103] = 0;   regs[37104] = 2300             # Phase B grid voltage
    regs[37105] = 0;   regs[37106] = 2300             # Phase C grid voltage
    for addr in range(37107, 37113):
        regs[addr] = 0                                 # Phase A/B/C currents (INT32, A×100)
    regs[37113] = 0;   regs[37114] = 0                # Grid active power (INT32, W) — updated live
    regs[37115] = 0;   regs[37116] = 0                # Reactive power (INT32, Var)
    regs[37117] = 1000                                 # Power factor (INT16, gain 1000 → 1.000)
    regs[37118] = 5000                                 # Grid frequency (INT16, Hz×100 → 50.00 Hz)
    regs[37119] = 0;   regs[37120] = 0                # Positive active energy (exported, INT32, kWh×100)
    regs[37121] = 0;   regs[37122] = 0                # Reverse active energy (imported, INT32, kWh×100)
    regs[37123] = 0;   regs[37124] = 0                # Cumulative reactive energy
    regs[37125] = 1                                    # Meter type: 1=three-phase DTSU666-H
    for addr in range(37132, 37138):
        regs[addr] = 0                                 # Phase A/B/C active power (INT32, W)

    # Battery unit 1 registers [MAP0 spec §3.2]
    regs[37000] = 2                                    # Storage running status: 2=running
    regs[37001] = 0;   regs[37002] = 0                # Storage unit 1 power (INT32, W) — updated live
    regs[37003] = 7940                                 # Battery bus voltage (confirmed from real inverter)
    regs[37004] = 0                                    # Storage unit 1 SoC (% × 10) — updated live
    regs[37046] = 0;   regs[37047] = 5000             # Max charge power (UINT32, W) → 5000W
    regs[37048] = 0;   regs[37049] = 5000             # Max discharge power (UINT32, W) → 5000W
    regs[37758] = 0;   regs[37759] = 20700            # Combined ESU max power (UINT32, W)
    regs[37760] = 0                                    # Combined ESU SOC (UINT16, % × 10) — updated live
    regs[37762] = 2                                    # ESU running status: 2=running
    regs[37765] = 0;   regs[37766] = 0                # Combined ESU charge/discharge power (INT32, W)

    # Battery unit 2 registers (not installed)
    regs[37738] = 0                                    # Battery unit 2 SOC (0=not present)
    regs[37741] = 0                                    # Battery unit 2 running status (0=offline)
    regs[37743] = 0;   regs[37744] = 0                # Battery unit 2 charge/discharge power (INT32)
    regs[47107] = 1                                    # Battery unit 1 — confirmed from real inverter
    regs[47108] = 0                                    # Battery unit 2 — 0=not installed

    # SunSpec header + model 1 (Common) + model 103 (Three-phase inverter) + end marker
    for addr in range(40000, 40124):
        regs[addr] = 0
    regs[40000] = 0x5375;  regs[40001] = 0x6E53      # "SunS" identifier
    regs[40002] = 1;        regs[40003] = 66           # Model 1, len=66
    regs[40070] = 103;      regs[40071] = 50           # Model 103, len=50
    regs[40085] = 0                                    # W_SF = 0 → watts (scale ×1)
    regs[40122] = 0xFFFF;   regs[40123] = 0           # End marker

    return regs


class RegisterBank:
    """Keeps the shared register dict up-to-date with latest sensor values."""

    def __init__(self, regs: dict):
        self._r = regs

    def _set_int32(self, addr: int, val: int):
        words = _pack_int32(val)
        self._r[addr] = words[0];  self._r[addr + 1] = words[1]

    def _set_uint16(self, addr: int, val: int):
        self._r[addr] = _pack_uint16(val)[0]

    def write_diagnostic(self):
        """Write unique fingerprint values to key registers for identification.

        Extended mode is triggered by the startup delay (15s of zeros at boot).
        By the time write_diagnostic() is called, extended mode is already established
        and sticky — so reg[32080] and reg[30071] can hold non-zero fingerprints safely.

        NOTE: Nibe treats reg[32080]=0 as "inverter offline" and hides ALL solar values
        on its display (even battery and grid). So 32080 MUST be non-zero here.

        Read the Nibe display and match the shown value to the register below:
          30071        → 1111   (UINT16;            Nibe shows ?)
          32016        → 3131   (PV1 voltage;       Nibe shows 313.1 V)
          32018        → 3232   (PV2 voltage;       Nibe shows 323.2 V)
          32064–32065  → 2222   (INT32 DC input;    Nibe shows 2.222 kW)
          32080        → 111    (UINT16 ×10W;       Nibe "Produced power"    = 111×10W = 1110W = 1.11 kW)
          32081        → 4444   (UINT16 W;          Nibe "Inverter capacity" = 4444/1000 = 4.444 kW)
          37113–37114  → 5555   (INT32 grid power;  Nibe shows 5.555 kW)
          37132–37133  → 6666   (INT32 house load;  Nibe shows 6.666 kW)
          37760        → 777    (UINT16 SOC ×10;    Nibe shows 77.7 %)
          37765–37766  → 8888   (INT32 batt power;  Nibe shows 8.888 kW)
        """
        self._set_uint16(30071, 1111)
        self._r[HUAWEI_REG_PV1_VOLTAGE] = 3131    # 32016: PV1 voltage (313.1 V)
        self._r[HUAWEI_REG_PV2_VOLTAGE] = 3232    # 32018: PV2 voltage (323.2 V)
        self._set_int32(32064,  2222)
        # Write 32080/32081 INDEPENDENTLY (not as INT32) — they are two separate UINT16 fields:
        #   reg[32080] × 10W → "Produced power"    (expected: 111×10W = 1.11 kW on Nibe display)
        #   reg[32081] / 1000 → "Inverter capacity" (expected: 4444/1000 = 4.444 kW on Nibe display)
        self._r[HUAWEI_REG_ACTIVE_PWR]     = 111   # 32080: 111 × 10W = 1110W = 1.11 kW
        self._r[HUAWEI_REG_ACTIVE_PWR + 1] = 4444  # 32081: 4444 W = 4.444 kW
        self._set_int32(37113,  5555)
        self._set_int32(37132,  6666)
        self._set_uint16(37760, 777)
        self._set_int32(37765,  8888)
        log.info("DIAGNOSTIC: 30071=1111  32016=3131(313.1V)  32018=3232(323.2V)  32064=2222  "
                 "32080=111(→1.11kW prod)  32081=4444(→4.444kW cap)  "
                 "37113=5555  37132=6666  37760=777(77.7%)  37765=8888")

    def _set_uint32(self, addr: int, val: int):
        clamped = max(0, min(0xFFFFFFFF, val))
        self._r[addr] = (clamped >> 16) & 0xFFFF
        self._r[addr + 1] = clamped & 0xFFFF

    def update(self, data: dict):
        """Write latest values into Modbus registers.  None = keep previous."""
        pv_w        = data.get("pv")
        batt_w      = data.get("batt")
        soc_pct     = data.get("soc")
        grid_w      = data.get("grid")
        active_pwr_w = data.get("active_pwr")

        # House consumption: active_power (AC output) ± grid (positive=export, negative=import)
        #   house = active_power − grid_export  (or + grid_import)
        active_pwr_for_house = active_pwr_w if active_pwr_w is not None else pv_w
        house_load: Optional[int] = None
        if None not in (active_pwr_for_house, grid_w):
            house_load = max(0, int(round(active_pwr_for_house - grid_w)))
        elif data.get("load") is not None:
            house_load = max(0, int(round(data["load"])))

        if pv_w is not None:
            v = int(round(pv_w))
            v16 = max(0, min(0xFFFF, v))
            self._set_int32(HUAWEI_REG_DC_INPUT, v)          # 32064: total DC input [MAP0 #135]
            # Nibe reads reg[32080] and reg[32081] as TWO INDEPENDENT UINT16s:
            #   "Produced power"    = reg[32080] × 10 W  (10W resolution)
            #   "Inverter capacity" = reg[32081] / 1000 kW  (1W resolution)
            # Writing independently (NOT as INT32) is required to make both correct.
            self._r[HUAWEI_REG_ACTIVE_PWR]     = max(0, min(0xFFFF, v // 10))  # 32080: v in 10W units
            self._r[HUAWEI_REG_ACTIVE_PWR + 1] = v16                            # 32081: v in W
            self._set_uint16(SUNSPEC_M103_W, v16)             # 40084: SunSpec Model 103 W
            self._set_uint16(NIBE_REG_PV_POWER, v16)          # 30071: legacy compat

        if grid_w is not None:
            self._set_int32(HUAWEI_REG_GRID_POWER, int(round(grid_w)))

        if soc_pct is not None:
            self._set_uint16(HUAWEI_REG_BATT_SOC, int(round(soc_pct * 10)))
            self._set_uint16(HUAWEI_REG_STORAGE_SOC, int(round(soc_pct * 10)))

        if batt_w is not None:
            self._set_int32(HUAWEI_REG_BATT_POWER, int(round(batt_w)))
            self._set_int32(HUAWEI_REG_STORAGE_POWER, int(round(batt_w)))

        batt_max_chg = data.get("batt_max_chg")
        if batt_max_chg is not None:
            self._set_uint32(NIBE_REG_BATT_MAX_CHG, int(round(batt_max_chg)))  # 37046, UINT32
        batt_max_dis = data.get("batt_max_dis")
        if batt_max_dis is not None:
            self._set_uint32(NIBE_REG_BATT_MAX_DIS, int(round(batt_max_dis)))  # 37048, UINT32

        # Nibe reads reg[32106-32107] as UINT32/100 and displays it as "Consumption kW".
        # Scale: house_load W // 10 → stored as 10W units → /100 = kW on display.
        # Example: 5240W // 10 = 524 → 524/100 = 5.24 kW shown as "Consumption".
        # reg[37132] is also written as INT32 W for other Nibe internal use.
        if house_load is not None:
            self._set_uint32(HUAWEI_REG_TOTAL_YIELD, max(0, house_load // 10))  # 32106: Consumption display
            self._set_int32(37132, house_load)

        # PV string DC voltages — MBSA V1: 32016=VPV-1, 32018=VPV-2 (gain 10 → e.g. 350.5V → 3505)
        for key, reg in [("pv1_v", HUAWEI_REG_PV1_VOLTAGE), ("pv2_v", HUAWEI_REG_PV2_VOLTAGE)]:
            v = data.get(key)
            if v is not None:
                self._set_uint16(reg, int(round(v * 10)))

        # Energy counters (UINT32, gain 100 → e.g. 123.45 kWh → 12345)
        daily = data.get("daily_kwh")
        if daily is not None:
            self._set_uint32(HUAWEI_REG_DAILY_YIELD, int(round(daily * 100)))

        # total_kwh (total lifetime yield) intentionally NOT written to reg[32106]
        # because Nibe uses that register for "Consumption" display (house load).
        # total_kwh sensor can remain configured but is unused in register writes.

        export_kwh = data.get("export_kwh")
        if export_kwh is not None:
            self._set_int32(HUAWEI_REG_GRID_EXPORT, int(round(export_kwh * 100)))

        import_kwh = data.get("import_kwh")
        if import_kwh is not None:
            self._set_uint32(HUAWEI_REG_GRID_IMPORT, int(round(import_kwh * 100)))

        # Temperature (INT16, gain 10 → e.g. 35.2°C → 352)
        temp = data.get("temp")
        if temp is not None:
            self._set_uint16(HUAWEI_REG_TEMPERATURE, int(round(temp * 10)) & 0xFFFF)

        # Power factor (INT16, gain 1000 → e.g. 0.95 → 950)
        pf = data.get("pf")
        if pf is not None:
            self._set_uint16(HUAWEI_REG_POWER_FACTOR, int(round(pf * 1000)) & 0xFFFF)

        # Grid frequency: hardcode 50.00 Hz (UINT16, gain 100 → 5000)
        self._set_uint16(HUAWEI_REG_GRID_FREQ, 5000)

        log.debug(
            f"RegisterBank updated: pv={pv_w} grid={grid_w} soc={soc_pct} batt={batt_w} "
            f"daily={daily}"
        )


# ---------------------------------------------------------------------------
# HA klient
# ---------------------------------------------------------------------------

class HAClient:
    """Jednoduchý async klient pre HA REST API cez Supervisor proxy."""

    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get_state(
        self, session: aiohttp.ClientSession, entity_id: str
    ) -> Optional[float]:
        url = f"{HA_BASE_URL}/api/states/{entity_id}"
        raw = None
        try:
            async with session.get(url, headers=self._headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.warning(f"get_state {entity_id}: HTTP {resp.status}")
                    return None
                data = await resp.json()
                raw = data.get("state", "unavailable")
                if raw in ("unavailable", "unknown", ""):
                    log.debug(f"{entity_id} = {raw}, preskakujem")
                    return None
                return float(raw)
        except (ValueError, TypeError) as e:
            log.warning(f"{entity_id}: nepodarilo sa konvertovať '{raw}' na float: {e}")
            return None
        except Exception as e:
            log.error(f"Chyba pri čítaní {entity_id}: {e}")
            return None

    async def call_service(
        self,
        session: aiohttp.ClientSession,
        domain: str,
        service: str,
        data: dict,
    ) -> bool:
        url = f"{HA_BASE_URL}/api/services/{domain}/{service}"
        try:
            async with session.post(url, headers=self._headers, json=data,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                ok = resp.status in (200, 201)
                if not ok:
                    body = await resp.text()
                    log.warning(
                        f"Služba {domain}.{service} zlyhala: HTTP {resp.status} – {body[:200]}"
                    )
                return ok
        except Exception as e:
            log.error(f"Chyba pri volaní {domain}.{service}: {e}")
            return False

    async def write_modbus_register(
        self,
        session: aiohttp.ClientSession,
        hub: str,
        address: int,
        value: int,
    ) -> bool:
        log.debug(f"Modbus write → hub={hub} addr={address} val={value}")
        return await self.call_service(
            session,
            "modbus",
            "write_register",
            {"hub": hub, "unit": 1, "address": address, "value": value},
        )


# ---------------------------------------------------------------------------
# Stav surplus logiky
# ---------------------------------------------------------------------------

@dataclass
class SurplusState:
    hw_mode: int = -1        # -1 = neznámy (pri štarte vždy zapíšeme)
    heating_offset: int = -99
    below_count: int = 0


# ---------------------------------------------------------------------------
# Hlavná trieda
# ---------------------------------------------------------------------------

class NibeHuaweiBridge:

    def __init__(self, opts: dict, bank: Optional["RegisterBank"] = None):
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            log.error("SUPERVISOR_TOKEN nie je nastavený – addon musí bežať v HA Supervisor")
            sys.exit(1)

        self._ha = HAClient(token)
        self._state = SurplusState()
        self._bank = bank
        self._interval: int = opts.get("update_interval", 30)

        s = opts.get("sensors", {})
        self._sensor_pv   = s.get("pv_power",      "sensor.emma_pv_output_power")
        self._sensor_soc  = s.get("battery_soc",   "sensor.emma_state_of_capacity")
        self._sensor_batt = s.get("battery_power", "sensor.emma_battery_charge_discharge_power")
        self._sensor_grid = s.get("grid_power",    "sensor.emma_feed_in_power")

        # Optional sensors — empty string means disabled
        self._optional_sensors = {}
        opt_map = {
            "active_pwr":   "active_power",
            "load":         "load_power",
            "daily_kwh":    "daily_yield_kwh",
            "total_kwh":    "total_yield_kwh",
            "export_kwh":   "grid_export_today_kwh",
            "import_kwh":   "grid_import_today_kwh",
            "temp":         "inverter_temp",
            "pf":           "power_factor",
            "pv1_v":        "pv1_voltage",
            "pv2_v":        "pv2_voltage",
            "batt_max_chg": "battery_max_charge_power",
            "batt_max_dis": "battery_max_discharge_power",
        }
        for key, cfg_name in opt_map.items():
            entity_id = s.get(cfg_name, "")
            if entity_id:
                self._optional_sensors[key] = entity_id

        self._diagnostic_mode = opts.get("diagnostic_mode", False)

        tm = opts.get("test_mode", {})
        self._test_mode    = tm.get("enabled", False)
        self._test_pv      = float(tm.get("pv_power",      0))
        self._test_soc     = float(tm.get("battery_soc",   80))
        self._test_batt    = float(tm.get("battery_power", -500))
        self._test_grid    = float(tm.get("grid_power",    -200))

        sc = opts.get("surplus_control", {})
        self._sc_enabled      = sc.get("enabled", False)
        self._hub             = sc.get("nibe_hub", "nibe")
        self._tuv_normal_w    = sc.get("tuv_normal_w",      1000)
        self._tuv_luxury_w    = sc.get("tuv_luxury_w",      3000)
        self._heat_thresh_w   = sc.get("heating_offset_w",  5000)
        self._heat_offset_val = sc.get("heating_offset_value", 3)
        self._hysteresis      = sc.get("hysteresis_cycles", 3)

    # ------------------------------------------------------------------
    # Čítanie dát z HA
    # ------------------------------------------------------------------

    async def _read_data(self, session: aiohttp.ClientSession) -> dict:
        # Core sensors (always read)
        pv, soc, batt, grid = await asyncio.gather(
            self._ha.get_state(session, self._sensor_pv),
            self._ha.get_state(session, self._sensor_soc),
            self._ha.get_state(session, self._sensor_batt),
            self._ha.get_state(session, self._sensor_grid),
        )
        result = {"pv": pv, "soc": soc, "batt": batt, "grid": grid}

        # Optional sensors (read in parallel)
        if self._optional_sensors:
            keys = list(self._optional_sensors.keys())
            values = await asyncio.gather(
                *(self._ha.get_state(session, eid) for eid in self._optional_sensors.values())
            )
            for k, v in zip(keys, values):
                result[k] = v

        return result

    # ------------------------------------------------------------------
    # Výpočet prebytku
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_surplus(data: dict) -> float:
        grid = data.get("grid")
        if grid is not None:
            return -grid
        return data.get("pv") or 0.0

    # ------------------------------------------------------------------
    # Surplus riadenie (HW comfort mode + heating offset)
    # ------------------------------------------------------------------

    async def _update_surplus_control(
        self, session: aiohttp.ClientSession, surplus: float
    ):
        if surplus >= self._heat_thresh_w:
            target_hw = 2
            target_offset = self._heat_offset_val
            self._state.below_count = 0
        elif surplus >= self._tuv_luxury_w:
            target_hw = 2
            target_offset = 0
            self._state.below_count = 0
        elif surplus >= self._tuv_normal_w:
            target_hw = 1
            target_offset = 0
            self._state.below_count = 0
        else:
            self._state.below_count += 1
            if self._state.below_count < self._hysteresis:
                log.debug(
                    f"Prebytok {surplus:.0f}W pod prahom, "
                    f"hysteréza {self._state.below_count}/{self._hysteresis}"
                )
                return
            target_hw = 0
            target_offset = 0
            self._state.below_count = 0

        if target_hw != self._state.hw_mode:
            mode_names = {0: "ECO", 1: "Normal", 2: "Luxury"}
            log.info(
                f"HW comfort mode: {mode_names.get(self._state.hw_mode, '?')} → "
                f"{mode_names[target_hw]}  (prebytok={surplus:.0f}W)"
            )
            if await self._ha.write_modbus_register(
                session, self._hub, REG_HW_COMFORT_MODE, target_hw
            ):
                self._state.hw_mode = target_hw

        if target_offset != self._state.heating_offset:
            log.info(
                f"Heating offset: {self._state.heating_offset} → {target_offset}"
                f"  (prebytok={surplus:.0f}W)"
            )
            reg_val = target_offset & 0xFFFF if target_offset < 0 else target_offset
            if await self._ha.write_modbus_register(
                session, self._hub, REG_HEATING_OFFSET, reg_val
            ):
                self._state.heating_offset = target_offset

    # ------------------------------------------------------------------
    # Hlavná slučka
    # ------------------------------------------------------------------

    async def run(self):
        log.info("=" * 60)
        log.info("Nibe-Huawei Bridge štartuje")
        log.info(f"  Interval:           {self._interval}s")
        log.info(f"  Modbus server:      {'zapnutý' if self._bank else 'vypnutý'}")
        log.info(f"  Extra sensors:      {len(self._optional_sensors)} configured")
        if self._optional_sensors:
            for key, eid in self._optional_sensors.items():
                log.info(f"    {key:18s} ← {eid}")
        log.info(f"  Surplus control:    {'zapnuté' if self._sc_enabled else 'vypnuté'}")
        if self._test_mode:
            log.info("  *** TEST MODE ***   HA sensors ignored, using hardcoded values:")
            log.info(f"    pv_power:         {self._test_pv:.0f}W")
            log.info(f"    battery_soc:      {self._test_soc:.0f}%")
            log.info(f"    battery_power:    {self._test_batt:.0f}W")
            log.info(f"    grid_power:       {self._test_grid:.0f}W")
        if self._sc_enabled:
            log.info(f"    Nibe hub:         {self._hub}")
            log.info(f"    TÚV Normal od:    {self._tuv_normal_w}W")
            log.info(f"    TÚV Luxury od:    {self._tuv_luxury_w}W")
            log.info(f"    Heating offset od:{self._heat_thresh_w}W (+{self._heat_offset_val})")
            log.info(f"    Hysteréza:        {self._hysteresis} cyklov")
        log.info("=" * 60)

        # Startup delay — keep all registers at 0 for 15 seconds so Nibe S1255 can
        # connect and read the standby state (reg[32080]=[0,0] AND reg[30071]=0).
        # This triggers extended polling mode, which is sticky for the session.
        # Skip delay in test mode (no Modbus server needed) or when bank is disabled.
        if self._bank is not None and not self._test_mode:
            STARTUP_DELAY = 15
            log.info(f"Startup delay: {STARTUP_DELAY}s — all registers at 0, "
                     "waiting for Nibe to enter extended polling mode...")
            await asyncio.sleep(STARTUP_DELAY)
            log.info("Startup delay done, starting normal polling loop.")

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    if self._diagnostic_mode:
                        if self._bank is not None:
                            self._bank.write_diagnostic()
                        await asyncio.sleep(self._interval)
                        continue

                    if self._test_mode:
                        data = {
                            "pv": self._test_pv, "batt": self._test_batt,
                            "soc": self._test_soc, "grid": self._test_grid,
                        }
                    else:
                        data = await self._read_data(session)

                    pv   = data.get("pv")
                    batt = data.get("batt")
                    soc  = data.get("soc")
                    grid = data.get("grid")

                    surplus = self._calc_surplus(data)

                    log.info(
                        f"FV: {pv or 0:.0f}W | "
                        f"Batéria: {batt or 0:.0f}W ({soc or 0:.0f}%) | "
                        f"Sieť: {grid or 0:.0f}W | "
                        f"Prebytok: {surplus:.0f}W"
                    )

                    if self._bank is not None:
                        self._bank.update(data)

                    if self._sc_enabled:
                        await self._update_surplus_control(session, surplus)

                except Exception as e:
                    log.error(f"Neočakávaná chyba v cykle: {e}", exc_info=True)

                await asyncio.sleep(self._interval)


# ---------------------------------------------------------------------------
# MITM Modbus TCP proxy
# ---------------------------------------------------------------------------

def _decode_fc3_request(pdu: bytes) -> str:
    if len(pdu) >= 5 and pdu[0] == 0x03:
        addr, count = struct.unpack(">HH", pdu[1:5])
        return f"FC3 ReadHolding addr={addr} count={count}"
    return f"FC={pdu[0]:#04x} len={len(pdu)}"


def _decode_fc3_response(pdu: bytes) -> str:
    if len(pdu) >= 2 and pdu[0] == 0x03:
        byte_count = pdu[1]
        regs = [struct.unpack(">H", pdu[2+i:4+i])[0] for i in range(0, byte_count, 2)]
        return f"FC3 Response {len(regs)} regs: {regs}"
    return f"FC={pdu[0]:#04x} len={len(pdu)}"


async def _mitm_handle(nibe_reader, nibe_writer, upstream_host, upstream_port,
                       nibe_unit_id, upstream_unit_id):
    peer = nibe_writer.get_extra_info("peername")
    log.info(f"MITM: Nibe connected from {peer}")
    try:
        inv_reader, inv_writer = await asyncio.open_connection(upstream_host, upstream_port)
    except Exception as e:
        log.error(f"MITM: cannot connect to inverter: {e}")
        nibe_writer.close()
        return
    log.info(f"MITM: forwarding to {upstream_host}:{upstream_port} "
             f"(unit {nibe_unit_id}→{upstream_unit_id})")

    async def forward(src: asyncio.StreamReader, dst: asyncio.StreamWriter,
                      src_unit: int, dst_unit: int, label: str):
        try:
            while True:
                header = await src.readexactly(7)
                tid, proto, length, unit = struct.unpack(">HHHB", header)
                payload = await src.readexactly(length - 1)
                rewritten = struct.pack(">HHHB", tid, proto, length, dst_unit) + payload
                if label == "→":
                    log.info(f"MITM Nibe→Inv  unit={unit}→{dst_unit} {_decode_fc3_request(payload)}")
                else:
                    log.info(f"MITM Inv→Nibe  unit={unit}→{dst_unit} {_decode_fc3_response(payload)}")
                dst.write(rewritten)
                await dst.drain()
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            log.debug(f"MITM forward [{label}] ended: {e}")

    await asyncio.gather(
        forward(nibe_reader, inv_writer, nibe_unit_id, upstream_unit_id, "→"),
        forward(inv_reader, nibe_writer, upstream_unit_id, nibe_unit_id, "←"),
    )
    log.info("MITM: session closed")
    inv_writer.close()
    nibe_writer.close()


async def run_mitm_proxy(listen_host: str, listen_port: int,
                         upstream_host: str, upstream_port: int,
                         nibe_unit_id: int, upstream_unit_id: int):
    handler = lambda r, w: _mitm_handle(r, w, upstream_host, upstream_port,
                                         nibe_unit_id, upstream_unit_id)
    server = await asyncio.start_server(handler, listen_host, listen_port)
    log.info(f"MITM proxy listening on {listen_host}:{listen_port} → "
             f"{upstream_host}:{upstream_port}")
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# Custom Modbus TCP server (replaces pymodbus ModbusTcpServer)
# ---------------------------------------------------------------------------

# Registers logged at INFO level so polling activity is always visible
_INFO_REGS = {30071, 32016, 32064, 32065, 32080, 32081, 37113, 37114, 37132, 37133, 37760, 37765, 37766}


class SimpleModbusTcpServer:
    """
    Minimal asyncio Modbus TCP server — handles FC3 (Read Holding Registers)
    from a plain dict[int, int].  Replaces pymodbus ModbusTcpServer.

    Key improvements over pymodbus approach:
    - Logs raw bytes per TCP segment BEFORE frame parsing (full diagnostic visibility)
    - Sends proper Modbus exception response for count=0 (pymodbus silently dropped it)
    - Handles count>125 with an exception response instead of silent drop
    - No hidden framing bugs — we own the loop
    """

    def __init__(self, host: str, port: int, unit_id: int, regs: dict):
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._regs = regs
        self._server = None

    async def serve_forever(self):
        try:
            self._server = await asyncio.start_server(
                self._handle_client, self._host, self._port)
        except PermissionError:
            log.error(
                f"Nepodarilo sa otvoriť port {self._port} – porty < 1024 vyžadujú root. "
                "Zmeňte modbus_server.port na 5020 alebo spustite ako root."
            )
            sys.exit(1)
        log.info(f"Modbus TCP server (custom) na {self._host}:{self._port} "
                 f"(unit_id={self._unit_id})")
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        log.info(f"Nibe connected from {peer}")
        buf = b""
        try:
            while True:
                chunk = await reader.read(256)
                if not chunk:
                    break
                log.debug(f"TCP recv {len(chunk)}B: {chunk.hex(' ')}")
                buf += chunk
                while True:
                    if len(buf) < 6:
                        break  # need at least MBAP (TID+proto+length = 6 bytes)
                    tid, proto, length = struct.unpack(">HHH", buf[:6])
                    frame_end = 6 + length          # MBAP(6) + PDU body(length bytes)
                    if len(buf) < frame_end:
                        break  # frame not yet complete
                    frame = buf[:frame_end]
                    buf = buf[frame_end:]
                    resp = self._process_frame(frame, tid, proto, length)
                    if resp:
                        log.debug(f"TCP send {len(resp)}B: {resp.hex(' ')}")
                        writer.write(resp)
                        await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            log.debug(f"Client {peer} error: {e}")
        finally:
            log.info(f"Nibe disconnected from {peer}")
            writer.close()

    def _process_frame(self, frame: bytes, tid: int, proto: int, length: int) -> bytes:
        if length < 2:
            log.warning(f"Modbus frame too short: length={length}")
            return b""
        unit = frame[6]
        pdu = frame[7:]
        fc = pdu[0]
        log.debug(f"Modbus frame: TID={tid} unit={unit} FC={fc:#04x} PDU={pdu.hex()}")

        if unit != self._unit_id:
            # Wrong unit — Modbus spec says no response for gateway target device failure
            log.debug(f"Ignoring frame for unit={unit} (our unit={self._unit_id})")
            return b""

        if fc == 0x03:
            return self._fc3(tid, proto, unit, pdu)

        log.info(f"Unsupported FC={fc:#04x} from Nibe, sending IllegalFunction exception")
        return self._exception(tid, proto, unit, fc, 0x01)  # Illegal Function

    def _fc3(self, tid: int, proto: int, unit: int, pdu: bytes) -> bytes:
        if len(pdu) < 5:
            log.warning(f"FC3 PDU too short ({len(pdu)}B), sending IllegalDataValue")
            return self._exception(tid, proto, unit, 0x03, 0x03)

        addr, count = struct.unpack(">HH", pdu[1:5])

        if count == 0:
            # Nibe sends count=0 for addr=32016 (PV1 voltage) as a probe.
            # Real inverter responds with a valid empty FC3 (byte_count=0); Nibe then
            # continues polling.  An exception response causes Nibe to re-identify
            # from scratch and never reach battery registers.
            log.info(f"FC3 count=0 addr={addr} — returning valid empty response (real inverter behaviour)")
            resp_pdu = bytes([0x03, 0x00])  # FC3, byte_count=0
            return struct.pack(">HHH", tid, proto, 1 + len(resp_pdu)) + bytes([unit]) + resp_pdu
        if count > 125:
            log.info(f"FC3 invalid count={count} addr={addr} — sending IllegalDataValue exception")
            return self._exception(tid, proto, unit, 0x03, 0x03)

        regs = [self._regs.get(addr + i, 0) for i in range(count)]
        polled = {addr + i for i in range(count)}
        if polled & _INFO_REGS:
            log.info(f"Nibe READ addr={addr} count={count} vals={regs}")
        else:
            log.debug(f"Nibe READ addr={addr} count={count} vals={regs}")

        data = b"".join(struct.pack(">H", r & 0xFFFF) for r in regs)
        resp_pdu = bytes([0x03, len(data)]) + data
        return struct.pack(">HHH", tid, proto, 1 + len(resp_pdu)) + bytes([unit]) + resp_pdu

    @staticmethod
    def _exception(tid: int, proto: int, unit: int, fc: int, code: int) -> bytes:
        resp_pdu = bytes([fc | 0x80, code])
        return struct.pack(">HHH", tid, proto, 1 + len(resp_pdu)) + bytes([unit]) + resp_pdu


# ---------------------------------------------------------------------------
# Async entry point
# ---------------------------------------------------------------------------

async def async_main(server: Optional[SimpleModbusTcpServer], bridge: NibeHuaweiBridge):
    tasks = [bridge.run()]
    if server is not None:
        tasks.append(server.serve_forever())
    await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Vstupný bod
# ---------------------------------------------------------------------------

def main():
    opts = load_options()

    log_level_str = opts.get("log_level", "info")
    log_level = LOG_LEVELS.get(log_level_str, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # MITM mode — bypass emulator, proxy directly to real inverter
    mitm = opts.get("mitm_mode", {})
    if mitm.get("enabled", False):
        ms = opts.get("modbus_server", {})
        listen_host = ms.get("host", "0.0.0.0")
        listen_port = ms.get("port", 5020)
        nibe_unit_id = ms.get("unit_id", 1)
        upstream_host = mitm.get("upstream_host", "192.168.68.79")
        upstream_port = mitm.get("upstream_port", 502)
        upstream_unit_id = mitm.get("upstream_unit_id", 3)
        log.info("=" * 60)
        log.info("MITM PROXY MODE — emulation disabled")
        log.info(f"  Listen:   {listen_host}:{listen_port} (unit {nibe_unit_id})")
        log.info(f"  Upstream: {upstream_host}:{upstream_port} (unit {upstream_unit_id})")
        log.info("  All register reads will be logged at INFO level")
        log.info("=" * 60)
        asyncio.run(run_mitm_proxy(listen_host, listen_port,
                                   upstream_host, upstream_port,
                                   nibe_unit_id, upstream_unit_id))
        return

    server = None
    bank = None

    ms = opts.get("modbus_server", {})
    if ms.get("enabled", True):
        unit_id        = ms.get("unit_id", 1)
        host           = ms.get("host", "0.0.0.0")
        port           = ms.get("port", 5020)
        rated_power_kw = int(opts.get("rated_power_kw", 10))

        regs = build_register_dict(rated_power_kw=rated_power_kw)
        bank = RegisterBank(regs)
        server = SimpleModbusTcpServer(host, port, unit_id, regs)

    bridge = NibeHuaweiBridge(opts, bank=bank)
    asyncio.run(async_main(server, bridge))


if __name__ == "__main__":
    main()
