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

# Older SUN2000 register map (MBSA V1/V2) — what Nibe S1255 actually polls
NIBE_REG_PV_POWER     = 30071  # UINT16, 1 reg, W — PV power (Nibe wall display "Produced power")
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
# RegisterBank – wraps pymodbus slave context for SUN2000 register layout
# ---------------------------------------------------------------------------

def build_modbus_context(unit_id: int, rated_power_kw: int = 10):
    """
    Build a ModbusServerContext with a sparse data block covering all known
    SUN2000 and SunSpec register addresses.  zero_mode=True so that register
    address N maps directly to data block index N (Huawei docs use 0-based).
    """
    from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
    from pymodbus.datastore.store import ModbusSparseDataBlock

    # Registers worth logging at DEBUG level to trace which reg maps to which display field
    _TRACE_REGS = {
        30071, 30073,                        # Nibe V1: PV power (W), rated power (kW)
        32064, 32065,                        # MAP0: DC input power from PV (INT32, W)
        32080, 32081,                        # MAP0: AC output active power (INT32, W)
        37113, 37114,                        # Grid active power (INT32, W)
        40083, 40084, 40085,                 # SunSpec W, W_SF
    }

    class ZeroDefaultSparseBlock(ModbusSparseDataBlock):
        """Returns 0 for any address not explicitly set (instead of IllegalAddress)."""
        def validate(self, address, count=1):
            return True
        def getValues(self, address, count=1):
            result = [self.values.get(address + i, 0) for i in range(count)]
            polled = {address + i for i in range(count)}
            if polled & _TRACE_REGS:
                log.debug(f"Nibe READ trace: addr={address} count={count} vals={result}")
            unknown = [address + i for i in range(count) if (address + i) not in self.values]
            if unknown:
                log.debug(f"Nibe polling unknown reg(s): addr={address} count={count} unknown={unknown}")
            log.debug(f"Nibe READ: addr={address} count={count} vals={result}")
            return result

    def _str_to_regs(s: str, num_regs: int) -> list[int]:
        """Encode ASCII string into Modbus registers (2 chars per register, big-endian)."""
        padded = s.ljust(num_regs * 2, "\x00")[:num_regs * 2]
        return [(ord(padded[i]) << 8) | ord(padded[i + 1]) for i in range(0, num_regs * 2, 2)]

    # Pre-populate all known addresses to 0
    initial: dict[int, int] = {}

    # SUN2000MA (MAP0) device identification block — MBSA V3.0 layout
    # 30000-30014: Model string (STRING30, 15 regs)
    model_regs = _str_to_regs("SUN2000-10K-MAP0", 15)
    for i, val in enumerate(model_regs):
        initial[30000 + i] = val
    # 30015-30024: Serial number (STRING20, 10 regs)
    sn_regs = _str_to_regs("BT2470706907", 10)
    for i, val in enumerate(sn_regs):
        initial[30015 + i] = val
    # 30025-30034: reserved zeros
    for addr in range(30025, 30035):
        initial[addr] = 0
    # 30035-30049: firmware version (STRING30, 15 regs)
    fw_regs = _str_to_regs("V200R024D02", 15)
    for i, val in enumerate(fw_regs):
        initial[30035 + i] = val
    # 30050-30064: software version (STRING30, 15 regs)
    sw_regs = _str_to_regs("V200R024C00SPC108", 15)
    for i, val in enumerate(sw_regs):
        initial[30050 + i] = val
    # 30065-30069: reserved zeros
    for addr in range(30065, 30070):
        initial[addr] = 0
    initial[30070] = 1004                            # 30070: model ID — 1004 = SUN2000_10K_MAP0 [MAP0 spec]
    initial[30071] = 0                               # 30071: active PV power (W, UINT16) — updated live

    # SUN2000 V3 data registers — ranges Nibe polls
    for addr in range(32000, 32002):   # 32000: device state
        initial[addr] = 0
    initial[32000] = 0x0002            # State 1: grid-connected normal
    for addr in range(32008, 32011):   # DC inputs (voltage/current)
        initial[addr] = 0
    for addr in range(32016, 32200):   # DC strings + AC outputs + misc (covers full scan)
        initial[addr] = 0
    initial[32089] = 0x0002            # Running state: grid-connected/running
    for addr in range(32064, 32066):   # Total DC input from PV (INT32, W) — updated live [MAP0 #135]
        initial[addr] = 0
    initial[32084] = 1000              # Power factor (INT16, gain 1000 → 1.000) [MAP0 #148]
    initial[32085] = 5000              # Grid frequency (UINT16, Hz×100 → 50.00 Hz) [MAP0 #149]
    for addr in range(32080, 32082):   # AC active power output (INT32, W) — updated live [MAP0 #146]
        initial[addr] = 0
    # Smart meter data registers [MAP0 spec §3.1 #261-281]
    # 37101-37102: Phase A grid voltage (INT32, V, gain 10 → 230.0V = 2300)
    initial[37101] = 0;    initial[37102] = 2300
    # 37103-37104: Phase B grid voltage
    initial[37103] = 0;    initial[37104] = 2300
    # 37105-37106: Phase C grid voltage
    initial[37105] = 0;    initial[37106] = 2300
    # 37107-37112: Phase A/B/C currents (INT32, A, gain 100) — zero until load known
    for addr in range(37107, 37113):
        initial[addr] = 0
    # 37113-37114: Grid active power (INT32, W, gain 1, +export/-import) — updated live
    initial[37113] = 0;    initial[37114] = 0
    # 37115-37116: Reactive power (INT32, Var, gain 1)
    initial[37115] = 0;    initial[37116] = 0
    # 37117: Power factor (INT16, gain 1000 → 1.000)
    initial[37117] = 1000
    # 37118: Grid frequency (INT16, Hz, gain 100 → 50.00 Hz = 5000)
    initial[37118] = 5000
    # 37119-37120: Positive active energy (exported, INT32, kWh, gain 100) — updated live
    initial[37119] = 0;    initial[37120] = 0
    # 37121-37122: Reverse active energy (imported, INT32, kWh, gain 100) — updated live
    initial[37121] = 0;    initial[37122] = 0
    # 37123-37124: Cumulative reactive energy
    initial[37123] = 0;    initial[37124] = 0
    # 37125: Meter type (1 = three-phase DTSU666-H)
    initial[37125] = 1
    # 37132-37137: Phase A/B/C active power (INT32, W, gain 1) — zero (meter data)
    for addr in range(37132, 37138):
        initial[addr] = 0
    # Battery presence / capability registers
    initial[37000] = 2                  # Storage running status: 2=running
    initial[37001] = 0                  # Storage unit 1 power (INT32 high) — updated live
    initial[37002] = 0                  # Storage unit 1 power (INT32 low) — updated live
    initial[37003] = 480                # Battery bus voltage (UINT16, gain 10 → 48.0V)
    initial[37004] = 0                  # Storage unit 1 SoC (% × 10) — updated live
    # Battery unit 1 — max charge/discharge power [MAP0 spec §3.2 #16/#17]
    initial[37046] = 0;  initial[37047] = 5000  # Max charge power (UINT32, W) → 5000W
    initial[37048] = 0;  initial[37049] = 5000  # Max discharge power (UINT32, W) → 5000W
    # Combined ESU (Energy Storage Unit) registers [MAP0 spec §3.2 #32-37]
    initial[37760] = 0                  # Combined ESU SOC (UINT16, % × 10) — updated live
    initial[37762] = 2                  # ESU running status: 2=running [MAP0 §3.2 #34]
    for addr in range(37765, 37767):   # Combined ESU charge/discharge power (INT32, W) — updated live
        initial[addr] = 0
    # Battery unit 2 registers — SN at 37700, SOC at 37738, status/power at 37741/37743
    initial[37738] = 0                  # Battery unit 2 SOC (% × 10, 0=not present)
    initial[37741] = 0                  # Battery unit 2 running status (0=offline)
    initial[37743] = 0;  initial[37744] = 0  # Battery unit 2 charge/discharge power (INT32, 0=no unit 2)
    initial[47107] = 150                 # Battery unit 1 capacity (kWh × 10 → 15.0 kWh, 3×5kWh packs)
    initial[47108] = 0                   # Battery unit 2 capacity — 0 = only one battery unit installed

    # Smart meter status [MAP0 spec §3.1 #260]
    initial[37100] = 1                  # Meter status: 1=normal

    # Rated power (so Nibe shows correct inverter capacity, not live production)
    # Set via options.rated_power_kw; passed in as parameter
    initial[30073] = rated_power_kw     # Rated power in kW (UINT16, gain 1)
    for addr in range(40000, 40124):   # SunSpec header + model 1 + model 103
        initial[addr] = 0
    initial[40085] = 0                  # SunSpec W_SF = 0 → W register is in watts (scale ×1)

    block = ZeroDefaultSparseBlock(initial)
    slave = ModbusSlaveContext(hr=block, zero_mode=True)
    ctx = ModbusServerContext(slaves={unit_id: slave}, single=False)

    # Seed SunSpec header
    _write_ctx(ctx, unit_id, 40000, [0x5375, 0x6E53])   # "SunS"
    _write_ctx(ctx, unit_id, 40002, [1, 66])              # Model 1, len=66
    _write_ctx(ctx, unit_id, 40070, [103, 50])            # Model 103, len=50
    _write_ctx(ctx, unit_id, 40122, [0xFFFF, 0])          # End marker

    return ctx


def _write_ctx(ctx, unit_id: int, addr: int, values: list[int]):
    """Write a list of 16-bit words into the server context at addr."""
    ctx[unit_id].setValues(3, addr, values)


class RegisterBank:
    """Keeps the pymodbus server context up-to-date with latest sensor values."""

    def __init__(self, ctx, unit_id: int):
        self._ctx = ctx
        self._uid = unit_id

    def _set_int32(self, addr: int, val: int):
        _write_ctx(self._ctx, self._uid, addr, _pack_int32(val))

    def _set_uint16(self, addr: int, val: int):
        _write_ctx(self._ctx, self._uid, addr, _pack_uint16(val))

    def _set_uint32(self, addr: int, val: int):
        """Pack unsigned int32 into two big-endian 16-bit Modbus words."""
        clamped = max(0, min(0xFFFFFFFF, val))
        _write_ctx(self._ctx, self._uid, addr, [(clamped >> 16) & 0xFFFF, clamped & 0xFFFF])

    def update(self, data: dict):
        """Write latest values into Modbus registers.  None = keep previous."""
        pv_w    = data.get("pv")
        batt_w  = data.get("batt")
        soc_pct = data.get("soc")
        grid_w  = data.get("grid")

        # House consumption via energy balance (batt_w>0=charging, grid_w>0=export):
        #   house = pv − battery_charge − grid_export
        # Compute first — used for both 30071 (kW, wall display) and 37101 (W, energy flow).
        house_load: Optional[int] = None
        if None not in (pv_w, batt_w, grid_w):
            house_load = max(0, int(round(pv_w - batt_w - grid_w)))
        elif data.get("load") is not None:
            house_load = max(0, int(round(data["load"])))

        if pv_w is not None:
            v = int(round(pv_w))
            self._set_int32(HUAWEI_REG_DC_INPUT, v)         # 32064: total DC input from PV [MAP0 spec #135]
            # 32080: AC output = PV ± battery (batt_w>0=charging reduces AC out)
            ac_out = v - int(round(batt_w)) if batt_w is not None else v
            self._set_int32(HUAWEI_REG_ACTIVE_PWR, ac_out)  # 32080: AC active power [MAP0 spec #146]
            self._set_uint16(SUNSPEC_M103_W, max(0, v) & 0xFFFF)       # 40084: SunSpec Model 103 W (PV production)
            # 30071: Nibe reads this for "Produced power" wall display (UINT16, W)
            self._set_uint16(NIBE_REG_PV_POWER, max(0, v))

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

        # Note: no dedicated house load register in MAP0 spec meter section.
        # house_load is computed but not written to any register (Nibe doesn't poll it from bridge).

        # PV string DC voltages — MBSA V1: 32016=VPV-1, 32018=VPV-2 (gain 10 → e.g. 350.5V → 3505)
        for key, reg in [("pv1_v", HUAWEI_REG_PV1_VOLTAGE), ("pv2_v", HUAWEI_REG_PV2_VOLTAGE)]:
            v = data.get(key)
            if v is not None:
                self._set_uint16(reg, int(round(v * 10)))

        # Energy counters (UINT32, gain 100 → e.g. 123.45 kWh → 12345)
        daily = data.get("daily_kwh")
        if daily is not None:
            self._set_uint32(HUAWEI_REG_DAILY_YIELD, int(round(daily * 100)))

        total = data.get("total_kwh")
        if total is not None:
            self._set_uint32(HUAWEI_REG_TOTAL_YIELD, int(round(total * 100)))

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
            f"daily={daily} total={total}"
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
            async with session.get(url, headers=self._headers) as resp:
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
            async with session.post(url, headers=self._headers, json=data) as resp:
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

        async with aiohttp.ClientSession() as session:
            while True:
                try:
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
# Async entry point
# ---------------------------------------------------------------------------

async def async_main(server_kwargs: Optional[dict], bridge: NibeHuaweiBridge):
    tasks = [bridge.run()]
    if server_kwargs is not None:
        from pymodbus.server import ModbusTcpServer
        try:
            server = ModbusTcpServer(**server_kwargs)
        except PermissionError:
            port = server_kwargs.get("address", ("", 0))[1]
            log.error(
                f"Nepodarilo sa otvoriť port {port} – porty < 1024 vyžadujú root. "
                "Zmeňte modbus_server.port na 5020 alebo spustite ako root."
            )
            sys.exit(1)
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
    if log_level == logging.DEBUG:
        logging.getLogger("pymodbus").setLevel(logging.DEBUG)

    # Nibe sometimes sends FC3 requests with count > 125 (Modbus max).
    # pymodbus rejects these with IllegalValue — suppress the noise since
    # the Nibe retries successfully with smaller counts.
    class _SuppressIllegalValue(logging.Filter):
        def filter(self, record):
            return "IllegalValue" not in record.getMessage()
    # Filters on a logger only intercept records emitted directly by that logger,
    # not records propagated from child loggers (e.g. pymodbus.server.async_io).
    # Add the filter to the root handlers so it catches everything that propagates up.
    _f = _SuppressIllegalValue()
    for _h in logging.root.handlers:
        _h.addFilter(_f)

    server_kwargs = None
    bank = None

    ms = opts.get("modbus_server", {})
    if ms.get("enabled", True):
        from pymodbus.device import ModbusDeviceIdentification

        unit_id        = ms.get("unit_id", 1)
        host           = ms.get("host", "0.0.0.0")
        port           = ms.get("port", 5020)
        rated_power_kw = int(opts.get("rated_power_kw", 10))

        ctx = build_modbus_context(unit_id, rated_power_kw=rated_power_kw)
        bank = RegisterBank(ctx, unit_id)

        identity = ModbusDeviceIdentification()
        identity.VendorName  = "Huawei Digital Power"
        identity.ProductCode = "SUN2000MA"
        identity.ModelName   = "SUN2000-10K-MAP0"
        identity.MajorMinorRevision = "V200R024C00SPC108"

        server_kwargs = {"context": ctx, "identity": identity, "address": (host, port)}
        log.info(f"Modbus TCP server na {host}:{port} (unit_id={unit_id})")

    bridge = NibeHuaweiBridge(opts, bank=bank)
    asyncio.run(async_main(server_kwargs, bridge))


if __name__ == "__main__":
    main()
