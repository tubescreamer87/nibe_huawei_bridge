#!/usr/bin/env python3
"""
Nibe-Huawei Bridge – Home Assistant Addon
==========================================
Číta stav Huawei FV invertera a batérie z HA a zapisuje dáta do Nibe S1255
cez HA Modbus integráciu.

Dva režimy (môžu bežať súbežne):
  1. external_registers – zapisuje surové W/% hodnoty do Nibe MODBUS40 registrov
     pre externé energetické dáta (Nibe potom sama riadi podľa prebytku)
  2. surplus_control – vypočítava prebytok a priamo nastavuje HW comfort mode
     a heating offset (fallback / doplnok k režimu 1)
"""

import asyncio
import aiohttp
import json
import logging
import os
import sys
from dataclasses import dataclass, field
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
# Nibe MODBUS40 register constants (S1255)
# Zdroj: Nibe MODBUS40 ModbusManager register list
# POZOR: Registre pre externé energetické dáta (43084–43088) OVERTE v
#        dokumentácii pre váš konkrétny firmware MODBUS40!
# ---------------------------------------------------------------------------

REG_HW_COMFORT_MODE = 47041   # 0=ECO, 1=Normal, 2=Luxury
REG_HEATING_OFFSET  = 47276   # Heating offset climate system (-10 až +10)
# Registre pre externé PV/batériové dáta – konfigurovateľné, defaulty nižšie:
REG_EXT_GRID_POWER    = 43084
REG_EXT_PV_POWER      = 43086
REG_EXT_BATT_POWER    = 43087
REG_EXT_BATT_SOC      = 43088

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
        """Vráti numerický stav entity alebo None ak nie je dostupná."""
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
        """Zapíše 16-bit register do HA Modbus hubu."""
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

    def __init__(self, opts: dict):
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            log.error("SUPERVISOR_TOKEN nie je nastavený – addon musí bežať v HA Supervisor")
            sys.exit(1)

        self._ha = HAClient(token)
        self._state = SurplusState()
        self._interval: int = opts.get("update_interval", 30)
        self._hub: str = opts.get("nibe_hub", "nibe")

        s = opts.get("sensors", {})
        self._sensor_pv   = s.get("pv_power",      "sensor.huawei_pv_power")
        self._sensor_soc  = s.get("battery_soc",   "sensor.huawei_battery_soc")
        self._sensor_batt = s.get("battery_power", "sensor.huawei_battery_power")
        self._sensor_grid = s.get("grid_power",    "sensor.huawei_grid_power")

        er = opts.get("nibe_external_registers", {})
        self._ext_enabled      = er.get("enabled", False)
        self._reg_pv           = er.get("pv_power",      REG_EXT_PV_POWER)
        self._reg_batt_power   = er.get("battery_power", REG_EXT_BATT_POWER)
        self._reg_batt_soc     = er.get("battery_soc",   REG_EXT_BATT_SOC)
        self._reg_grid         = er.get("grid_power",    REG_EXT_GRID_POWER)

        sc = opts.get("surplus_control", {})
        self._sc_enabled      = sc.get("enabled", True)
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
        """
        Prebytok = energia ktorú exportujeme do siete.
        Huawei grid_power: kladná = import zo siete, záporná = export do siete.
        Prebytok = -grid_power (keď exportujeme, je to pozitívne číslo).
        """
        grid = data.get("grid")
        if grid is not None:
            return -grid
        # Fallback keď nemáme grid sensor
        pv = data.get("pv") or 0.0
        return pv

    # ------------------------------------------------------------------
    # Zápis externých energetických registrov do Nibe
    # ------------------------------------------------------------------

    async def _write_external_registers(
        self, session: aiohttp.ClientSession, data: dict
    ):
        """
        Zapisuje surové FV/batériové dáta do Nibe MODBUS40 externých registrov.
        Nibe potom sama optimalizuje spotrebu podľa dostupnej energie.

        ⚠️  Registre musia byť overené v MODBUS40 dokumentácii pre váš firmware!
        """
        # Hodnoty škálujeme na celé čísla (Nibe registre sú 16-bit signed)
        writes: list[tuple[int, Optional[float]]] = [
            (self._reg_pv,         data.get("pv")),
            (self._reg_batt_power, data.get("batt")),
            (self._reg_batt_soc,   data.get("soc")),
            (self._reg_grid,       data.get("grid")),
        ]

        for address, raw_value in writes:
            if raw_value is None:
                continue
            int_val = int(round(raw_value))
            # 16-bit signed: záporné čísla cez two's complement
            if int_val < 0:
                int_val = int_val & 0xFFFF
            await self._ha.write_modbus_register(session, self._hub, address, int_val)

    # ------------------------------------------------------------------
    # Surplus riadenie (HW comfort mode + heating offset)
    # ------------------------------------------------------------------

    async def _update_surplus_control(
        self, session: aiohttp.ClientSession, surplus: float
    ):
        """
        Priame riadenie Nibe podľa prebytku:
          - TÚV comfort mode: ECO / Normal / Luxury
          - Heating offset: zvýšenie teploty pri veľkom prebytku
        """
        # Určíme cieľový stav
        if surplus >= self._heat_thresh_w:
            target_hw = 2               # Luxury
            target_offset = self._heat_offset_val
            self._state.below_count = 0
        elif surplus >= self._tuv_luxury_w:
            target_hw = 2               # Luxury
            target_offset = 0
            self._state.below_count = 0
        elif surplus >= self._tuv_normal_w:
            target_hw = 1               # Normal
            target_offset = 0
            self._state.below_count = 0
        else:
            # Pod prahom – hysteréza pred znížením
            self._state.below_count += 1
            if self._state.below_count < self._hysteresis:
                log.debug(
                    f"Prebytok {surplus:.0f}W pod prahom, "
                    f"hysteréza {self._state.below_count}/{self._hysteresis}"
                )
                return
            target_hw = 0               # ECO
            target_offset = 0
            self._state.below_count = 0

        # Zapíš iba ak sa stav zmenil
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
        log.info(f"  Nibe Modbus hub:    {self._hub}")
        log.info(f"  Externé registre:   {'zapnuté' if self._ext_enabled else 'vypnuté'}")
        log.info(f"  Surplus control:    {'zapnuté' if self._sc_enabled else 'vypnuté'}")
        if self._sc_enabled:
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

                    if self._ext_enabled:
                        await self._write_external_registers(session, data)

                    if self._sc_enabled:
                        await self._update_surplus_control(session, surplus)

                except Exception as e:
                    log.error(f"Neočakávaná chyba v cykle: {e}", exc_info=True)

                await asyncio.sleep(self._interval)


# ---------------------------------------------------------------------------
# Vstupný bod
# ---------------------------------------------------------------------------

def main():
    opts = load_options()

    log_level_str = opts.get("log_level", "info")
    logging.basicConfig(
        level=LOG_LEVELS.get(log_level_str, logging.INFO),
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    bridge = NibeHuaweiBridge(opts)
    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
