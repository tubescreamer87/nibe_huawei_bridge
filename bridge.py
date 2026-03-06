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

# Huawei SUN2000 proprietary Modbus registers (0-based)
HUAWEI_REG_PV_POWER   = 32080  # INT32, 2 regs, W (active AC output)  [MBSA V3]
HUAWEI_REG_GRID_POWER = 37113  # INT32, 2 regs, W (+export / -import) [MBSA V3]
HUAWEI_REG_BATT_SOC   = 37760  # UINT16, 1 reg, % * 10               [MBSA V3]
HUAWEI_REG_BATT_POWER = 37765  # INT32, 2 regs, W (+charge / -discharge) [MBSA V3]

# Older SUN2000 register map (MBSA V1/V2) — what Nibe S1255 actually polls
NIBE_REG_PV_POWER     = 30071  # UINT16, 1 reg, W — active power output [MBSA V1]
NIBE_REG_BATT_MAX_CHG = 37758  # UINT16, 1 reg, W — max charge power (not state!)
NIBE_REG_BATT_MAX_DIS = 37759  # UINT16, 1 reg, W — max discharge power
HUAWEI_REG_LOAD_POWER = 37101  # INT32, 2 regs, W — total load/consumption power

# SunSpec magic – populated so Nibe finds either proprietary or SunSpec regs
SUNSPEC_BASE          = 40000  # "SunS" identifier (2 regs)
SUNSPEC_MODEL1_BASE   = 40002  # Model 1 (Common), length 66
SUNSPEC_MODEL103_BASE = 40070  # Model 103 (Three-phase inverter), length 50
SUNSPEC_M103_W        = 40083  # Model 103 AC Power (INT16, 1 reg, W with scale)

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

def build_modbus_context(unit_id: int):
    """
    Build a ModbusServerContext with a sparse data block covering all known
    SUN2000 and SunSpec register addresses.  zero_mode=True so that register
    address N maps directly to data block index N (Huawei docs use 0-based).
    """
    from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
    from pymodbus.datastore.store import ModbusSparseDataBlock

    class ZeroDefaultSparseBlock(ModbusSparseDataBlock):
        """Returns 0 for any address not explicitly set (instead of IllegalAddress)."""
        def validate(self, address, count=1):
            return True
        def getValues(self, address, count=1):
            result = [self.values.get(address + i, 0) for i in range(count)]
            unknown = [address + i for i in range(count) if (address + i) not in self.values]
            if unknown:
                log.info(f"Nibe polling unknown reg(s): addr={address} count={count} unknown={unknown}")
            return result

    def _str_to_regs(s: str, num_regs: int) -> list[int]:
        """Encode ASCII string into Modbus registers (2 chars per register, big-endian)."""
        padded = s.ljust(num_regs * 2, "\x00")[:num_regs * 2]
        return [(ord(padded[i]) << 8) | ord(padded[i + 1]) for i in range(0, num_regs * 2, 2)]

    # Pre-populate all known addresses to 0
    initial: dict[int, int] = {}

    # SUN2000 device identification block (MBSA V1 register map, 30000-30071)
    model_regs = _str_to_regs("SUN2000-10KTL", 10)  # 30000-30009: model STRING20
    for i, val in enumerate(model_regs):
        initial[30000 + i] = val
    sn_regs = _str_to_regs("HA-NIBE-BRIDGE", 10)    # 30010-30019: SN STRING20
    for i, val in enumerate(sn_regs):
        initial[30010 + i] = val
    fw_regs = _str_to_regs("V200R002", 8)            # 30020-30027: firmware STRING16
    for i, val in enumerate(fw_regs):
        initial[30020 + i] = val
    initial[30028] = 1                               # 30028: device type (1 = string inverter)
    for addr in range(30029, 30071):                 # 30029-30070: reserved/status (0)
        initial[addr] = 0
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
    for addr in range(32080, 32082):   # PV power (INT32) — updated live
        initial[addr] = 0
    for addr in range(37101, 37120):   # Grid voltages/currents/frequency
        initial[addr] = 0
    for addr in range(37113, 37115):   # Grid power (INT32) — updated live
        initial[addr] = 0
    for addr in range(37132, 37138):   # Storage output/grid data
        initial[addr] = 0
    initial[37758] = 5000              # Max charge power (W) — realistic for LUNA2000
    initial[37759] = 5000              # Max discharge power (W)
    initial[37760] = 0                  # Battery SoC (UINT16) — updated live
    for addr in range(37765, 37767):   # Battery power (INT32) — updated live
        initial[addr] = 0
    initial[47107] = 0                  # Storage control param 1
    initial[47108] = 0                  # Storage control param 2
    for addr in range(40000, 40124):   # SunSpec header + model 1 + model 103
        initial[addr] = 0

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

    def update(
        self,
        pv_w: Optional[float],
        batt_w: Optional[float],
        soc_pct: Optional[float],
        grid_w: Optional[float],
    ):
        """Write latest values into Modbus registers.  None = keep previous."""
        if pv_w is not None:
            v = int(round(pv_w))
            self._set_int32(HUAWEI_REG_PV_POWER, v)
            self._set_uint16(NIBE_REG_PV_POWER, max(0, v))  # MBSA V1: UINT16, no negatives
            # SunSpec M103 AC Power (INT16, W, scale factor 0 at 40091)
            self._set_uint16(SUNSPEC_M103_W, v & 0xFFFF)

        if grid_w is not None:
            self._set_int32(HUAWEI_REG_GRID_POWER, int(round(grid_w)))

        if soc_pct is not None:
            # SUN2000 V3 stores SoC as % * 10
            self._set_uint16(HUAWEI_REG_BATT_SOC, int(round(soc_pct * 10)))

        if batt_w is not None:
            self._set_int32(HUAWEI_REG_BATT_POWER, int(round(batt_w)))

        # House consumption = PV production + battery discharge + grid import
        # batt_w: positive=charging, negative=discharging (Huawei EMMA convention)
        # grid_w: positive=export, negative=import (Huawei EMMA convention)
        if None not in (pv_w, batt_w, grid_w):
            consumption = max(0, int(round(
                max(0.0, pv_w) + max(0.0, -batt_w) + max(0.0, -grid_w)
            )))
            self._set_int32(HUAWEI_REG_LOAD_POWER, consumption)

        log.debug(
            f"RegisterBank updated: pv={pv_w} grid={grid_w} soc={soc_pct} batt={batt_w}"
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
        pv, soc, batt, grid = await asyncio.gather(
            self._ha.get_state(session, self._sensor_pv),
            self._ha.get_state(session, self._sensor_soc),
            self._ha.get_state(session, self._sensor_batt),
            self._ha.get_state(session, self._sensor_grid),
        )
        return {"pv": pv, "soc": soc, "batt": batt, "grid": grid}

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
        log.info(f"  Surplus control:    {'zapnuté' if self._sc_enabled else 'vypnuté'}")
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
                        self._bank.update(pv, batt, soc, grid)

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

    server_kwargs = None
    bank = None

    ms = opts.get("modbus_server", {})
    if ms.get("enabled", True):
        from pymodbus.device import ModbusDeviceIdentification

        unit_id = ms.get("unit_id", 1)
        host    = ms.get("host", "0.0.0.0")
        port    = ms.get("port", 5020)

        ctx = build_modbus_context(unit_id)
        bank = RegisterBank(ctx, unit_id)

        identity = ModbusDeviceIdentification()
        identity.VendorName  = "Huawei"
        identity.ProductCode = "SUN2000"
        identity.ModelName   = "SUN2000-10KTL"
        identity.MajorMinorRevision = "V200R002"

        server_kwargs = {"context": ctx, "identity": identity, "address": (host, port)}
        log.info(f"Modbus TCP server na {host}:{port} (unit_id={unit_id})")

    bridge = NibeHuaweiBridge(opts, bank=bank)
    asyncio.run(async_main(server_kwargs, bridge))


if __name__ == "__main__":
    main()
