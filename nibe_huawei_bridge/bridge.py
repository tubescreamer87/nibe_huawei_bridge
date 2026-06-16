#!/usr/bin/env python3
"""
Nibe-Huawei Bridge – Home Assistant Addon
==========================================
Emuluje Huawei EMMA (SmartHEMS) cez Modbus TCP.
Nibe S1255 sa pripojí priamo ako Modbus master a číta PV/batériové/sieťové dáta.

Dva režimy (môžu bežať súbežne):
  1. modbus_server – embedded Modbus TCP slave server mimiking EMMA (unit/logical-device-id 0)
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

# Huawei EMMA (SmartHEMS) Modbus registers — captured 1:1 from a real EMMA-A02
# (logical-device-id/unit = 0, TCP port 502) and cross-checked against the official
# "SmartHEMS V100R024C00 MODBUS Interface Definitions" spec and wlcrs/huawei-solar-lib.
# Power registers are plain watts (spec "kW × gain 1000" == raw W); SOC is % × 100;
# energy counters are kWh × 100; ESS capacities are kWh × 1000.

# Characteristic data (strings)
EMMA_REG_OFFERING_NAME = 30000  # STRING, 15 regs — "SmartHEMS"
EMMA_REG_SN            = 30015  # STRING, 10 regs — serial number
EMMA_REG_SOFTWARE_VER  = 30035  # STRING, 15 regs — "SmartHEMS V100R025C00SPC131"
EMMA_REG_MODEL         = 30222  # STRING, 20 regs — "EMMA-A02"

# Live sampled data
EMMA_REG_PV_POWER         = 30354  # U32, W  — PV output power
EMMA_REG_LOAD_POWER       = 30356  # U32, W  — house load power
EMMA_REG_FEED_IN_POWER    = 30358  # I32, W  — grid power (+ = export / − = import)
EMMA_REG_BATT_POWER       = 30360  # I32, W  — battery (+ = charge / − = discharge)
EMMA_REG_INV_RATED_POWER  = 30362  # U32, W  — inverter rated power
EMMA_REG_INV_ACTIVE_POWER = 30364  # I32, W  — inverter active power
EMMA_REG_SOC              = 30368  # U16, % × 100 — combined ESS state of capacity
EMMA_REG_ESS_CHG_CAP      = 30369  # U32, kWh × 1000 — ESS chargeable capacity
EMMA_REG_ESS_DIS_CAP      = 30371  # U32, kWh × 1000 — ESS dischargeable capacity
EMMA_REG_BACKUP_SOC       = 30373  # U16, % × 100 — backup power SOC

# Energy counters (kWh × 100)
EMMA_REG_ENERGY_CHARGED_TODAY    = 30306  # U32
EMMA_REG_ENERGY_DISCHARGED_TODAY = 30312  # U32
EMMA_REG_RATED_ESS_CAPACITY      = 30322  # U32, kWh × 1000
EMMA_REG_CONSUMPTION_TODAY       = 30324  # U32
EMMA_REG_FEED_IN_TODAY           = 30330  # U32 — feed-in to grid today
EMMA_REG_SUPPLY_FROM_GRID_TODAY  = 30336  # U32 — supply from grid today
EMMA_REG_INV_YIELD_TODAY         = 30342  # U32 — inverter energy yield today
EMMA_REG_INV_TOTAL_YIELD         = 30344  # U32 — inverter total energy yield
EMMA_REG_PV_YIELD_TODAY          = 30346  # U32 — PV yield today

# Device management
EMMA_REG_NUM_INVERTERS = 30801  # U16
EMMA_REG_NUM_CHARGERS  = 30804  # U16

# Public registers
EMMA_REG_DEVICE_CONN_STATUS = 65534  # U16 — device connection status

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
# RegisterBank – plain dict + helpers for the EMMA register layout
# ---------------------------------------------------------------------------

def _str_to_regs(s: str, num_regs: int) -> list[int]:
    """Encode ASCII string into Modbus registers (2 chars per register, big-endian)."""
    padded = s.ljust(num_regs * 2, "\x00")[:num_regs * 2]
    return [(ord(padded[i]) << 8) | ord(padded[i + 1]) for i in range(0, num_regs * 2, 2)]


def build_register_dict(rated_power_kw: int = 10) -> dict:
    """
    Build a plain dict[int, int] emulating a Huawei EMMA-A02 (SmartHEMS).
    Missing addresses return 0 at read time (no IllegalAddress).  Live values are
    filled in by RegisterBank.update(); identity/static values are set here from a
    register dump of the user's real EMMA.
    """
    regs: dict[int, int] = {}

    def _put_u32(addr: int, value: int):
        v = max(0, min(0xFFFFFFFF, value))
        regs[addr] = (v >> 16) & 0xFFFF
        regs[addr + 1] = v & 0xFFFF

    # --- Identity strings (captured 1:1 from the real EMMA-A02) ---
    for i, val in enumerate(_str_to_regs("SmartHEMS", 15)):
        regs[EMMA_REG_OFFERING_NAME + i] = val            # 30000-30014
    for i, val in enumerate(_str_to_regs("BT24B0106206", 10)):
        regs[EMMA_REG_SN + i] = val                       # 30015-30024
    for i, val in enumerate(_str_to_regs("SmartHEMS V100R025C00SPC131", 15)):
        regs[EMMA_REG_SOFTWARE_VER + i] = val             # 30035-30049
    for i, val in enumerate(_str_to_regs("EMMA-A02", 20)):
        regs[EMMA_REG_MODEL + i] = val                    # 30222-30241

    # --- Static / nameplate values ---
    _put_u32(EMMA_REG_INV_RATED_POWER, rated_power_kw * 1000)   # W
    _put_u32(EMMA_REG_RATED_ESS_CAPACITY, 20700)               # 20.700 kWh (×1000)
    _put_u32(EMMA_REG_ESS_CHG_CAP, 20700)
    _put_u32(EMMA_REG_ESS_DIS_CAP, 20700)
    regs[EMMA_REG_NUM_INVERTERS] = 1
    regs[EMMA_REG_NUM_CHARGERS] = 0
    regs[EMMA_REG_DEVICE_CONN_STATUS] = 0xB001                # observed on real EMMA

    # --- Live registers: pre-create at 0 so block reads never miss ---
    for reg in (EMMA_REG_PV_POWER, EMMA_REG_LOAD_POWER, EMMA_REG_FEED_IN_POWER,
                EMMA_REG_BATT_POWER, EMMA_REG_INV_ACTIVE_POWER,
                EMMA_REG_ENERGY_CHARGED_TODAY, EMMA_REG_ENERGY_DISCHARGED_TODAY,
                EMMA_REG_CONSUMPTION_TODAY, EMMA_REG_FEED_IN_TODAY,
                EMMA_REG_SUPPLY_FROM_GRID_TODAY, EMMA_REG_INV_YIELD_TODAY,
                EMMA_REG_INV_TOTAL_YIELD, EMMA_REG_PV_YIELD_TODAY):
        regs[reg] = 0
        regs[reg + 1] = 0
    regs[EMMA_REG_SOC] = 0
    regs[EMMA_REG_BACKUP_SOC] = 0

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

    def _set_uint32(self, addr: int, val: int):
        clamped = max(0, min(0xFFFFFFFF, val))
        self._r[addr] = (clamped >> 16) & 0xFFFF
        self._r[addr + 1] = clamped & 0xFFFF

    def write_diagnostic(self):
        """Write recognizable fingerprint values to the live EMMA registers so each
        Nibe display field can be matched to a register.  Disable when done."""
        self._set_uint32(EMMA_REG_PV_POWER,        1111)   # → 1.111 kW PV
        self._set_uint32(EMMA_REG_LOAD_POWER,      2222)   # → 2.222 kW load
        self._set_int32(EMMA_REG_FEED_IN_POWER,    3333)   # → 3.333 kW grid
        self._set_int32(EMMA_REG_BATT_POWER,       4444)   # → 4.444 kW battery
        self._set_int32(EMMA_REG_INV_ACTIVE_POWER, 5555)   # → 5.555 kW inverter
        self._set_uint16(EMMA_REG_SOC,             6666)   # → 66.66 % SOC
        log.info("DIAGNOSTIC EMMA: PV=1111W(30354) load=2222W(30356) grid=3333W(30358) "
                 "batt=4444W(30360) inv=5555W(30364) soc=66.66%(30368)")

    def update(self, data: dict):
        """Write latest values into EMMA Modbus registers.  None = keep previous."""
        pv_w         = data.get("pv")
        batt_w       = data.get("batt")
        soc_pct      = data.get("soc")
        grid_w       = data.get("grid")
        active_pwr_w = data.get("active_pwr")
        load_w       = data.get("load")

        if pv_w is not None:
            self._set_uint32(EMMA_REG_PV_POWER, max(0, int(round(pv_w))))

        # House load: prefer the configured load_power sensor; otherwise derive from
        # the energy balance house = pv − batt − grid (correct for all sign combos).
        if load_w is not None:
            self._set_uint32(EMMA_REG_LOAD_POWER, max(0, int(round(load_w))))
        elif None not in (pv_w, batt_w, grid_w):
            self._set_uint32(EMMA_REG_LOAD_POWER,
                             max(0, int(round(pv_w - batt_w - grid_w))))

        if grid_w is not None:
            self._set_int32(EMMA_REG_FEED_IN_POWER, int(round(grid_w)))   # +export/−import

        if batt_w is not None:
            self._set_int32(EMMA_REG_BATT_POWER, int(round(batt_w)))      # +charge/−discharge

        # Inverter active power: prefer the configured sensor; else approximate as
        # AC output = pv − battery_charge.
        if active_pwr_w is not None:
            self._set_int32(EMMA_REG_INV_ACTIVE_POWER, int(round(active_pwr_w)))
        elif None not in (pv_w, batt_w):
            self._set_int32(EMMA_REG_INV_ACTIVE_POWER, int(round(pv_w - batt_w)))

        if soc_pct is not None:
            self._set_uint16(EMMA_REG_SOC, int(round(soc_pct * 100)))     # % × 100

        # Energy counters (kWh × 100)
        for key, reg in [
            ("daily_kwh",  EMMA_REG_INV_YIELD_TODAY),
            ("total_kwh",  EMMA_REG_INV_TOTAL_YIELD),
            ("export_kwh", EMMA_REG_FEED_IN_TODAY),
            ("import_kwh", EMMA_REG_SUPPLY_FROM_GRID_TODAY),
        ]:
            v = data.get(key)
            if v is not None:
                self._set_uint32(reg, max(0, int(round(v * 100))))

        log.debug(
            f"RegisterBank(EMMA) updated: pv={pv_w} load={load_w} grid={grid_w} "
            f"batt={batt_w} soc={soc_pct} active={active_pwr_w}"
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
        self._sensor_pv   = s.get("pv_power",      "sensor.inverter_input_power")
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
        # grid_power convention (emma_feed_in_power): + = export, − = import.
        # Surplus available to divert = exported power = grid.
        grid = data.get("grid")
        if grid is not None:
            return grid
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

        # NOTE: the EMMA emulation needs no startup zero-hold (that was a SUN2000
        # "extended polling mode" trick); registers serve live values immediately.

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

# EMMA registers logged at INFO level so we can see exactly what Nibe polls.
#   30000/30015/30035/30222 → device identification strings
#   30354/30356/30358/30360 → PV / load / grid / battery power
#   30364/30368             → inverter active power / SOC
#   30330/30336/30342/30344 → energy counters
_INFO_REGS = {
    30000, 30015, 30035, 30222,                    # identity strings
    30354, 30356, 30358, 30360,                    # PV / load / grid / battery
    30362, 30364, 30368, 30373,                    # rated / active / SOC / backup SOC
    30330, 30336, 30342, 30344, 30346,             # energy counters
    30801, 30804,                                  # device counts
}


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
        if fc == 0x2B:
            return self._fc43(tid, proto, unit, pdu)

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

    # FC 0x2B / MEI 0x0E — Read Device Identification.  This is how Nibe (and
    # wlcrs/huawei-solar-lib) recognizes a device as an EMMA: vendor "Huawei",
    # product code "HEMS", and the extended device descriptor with key 8=HEMS.
    # Values captured 1:1 from the real EMMA-A02.
    _ID_BASIC = [
        (0x00, b"Huawei"),
        (0x01, b"HEMS"),
        (0x02, b"V100R025C00SPC131"),
    ]
    _ID_DEVLIST = [
        (0x87, bytes([1])),  # number of devices (we expose just the EMMA itself)
        (0x88, b"1=EMMA-A02;2=V100R025C00SPC131;3=P1.15-D1.0;"
               b"4=BT24B0106206;5=0;6=1.0;8=HEMS;9=0"),
    ]

    def _fc43(self, tid: int, proto: int, unit: int, pdu: bytes) -> bytes:
        if len(pdu) < 4 or pdu[1] != 0x0E:
            return self._exception(tid, proto, unit, 0x2B, 0x01)  # Illegal Function
        code = pdu[2]
        obj_id = pdu[3]
        log.info(f"Nibe FC43 ReadDeviceId code={code} obj={obj_id:#04x}")

        more = 0x00
        next_id = 0x00
        if code == 0x01:                              # basic — stream vendor/product/version
            objs = self._ID_BASIC
            conformity = 0x81
        elif code in (0x02, 0x03):                    # regular/extended — device list, stream from obj_id
            chain = self._ID_DEVLIST
            idx = next((i for i, (oid, _) in enumerate(chain) if oid == obj_id), None)
            if idx is None:
                single = dict(self._ID_BASIC).get(obj_id)
                if single is None:
                    return self._exception(tid, proto, unit, 0x2B, 0x02)  # Illegal Data Address
                objs = [(obj_id, single)]
            else:
                objs = [chain[idx]]
                if idx + 1 < len(chain):
                    more, next_id = 0xFF, chain[idx + 1][0]
            conformity = 0x83
        else:
            return self._exception(tid, proto, unit, 0x2B, 0x01)

        body = bytes([0x2B, 0x0E, code, conformity, more, next_id, len(objs)])
        for oid, val in objs:
            body += bytes([oid, len(val)]) + val
        return struct.pack(">HHH", tid, proto, 1 + len(body)) + bytes([unit]) + body

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
