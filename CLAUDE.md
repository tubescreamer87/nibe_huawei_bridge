# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Home Assistant addon that emulates a Huawei SUN2000 inverter over Modbus TCP. Nibe S1255 connects to the bridge directly as a Modbus master and polls PV/battery/grid registers. The bridge reads real values from HA sensor entities via Supervisor REST API and serves them from an embedded pymodbus TCP server.

## Development Commands

Dependencies: `aiohttp`, `pymodbus` (installed via pip in Dockerfile). No test framework or linter. To run locally:

```bash
python3 bridge.py
```

Requires `SUPERVISOR_TOKEN` env var and a running HA instance. For Docker builds, the addon uses multi-arch HA base images defined in `build.yaml`.

To install as a local HA addon: copy the repo folder to `/config/addons/` on your HA host, then reload addon store.

## Architecture

Single file: `bridge.py`. Key classes and functions:

- **`build_modbus_context(unit_id)`** — builds a `ModbusSparseDataBlock`-backed server context with Huawei proprietary (32080–37766) and SunSpec (40000–40123) registers pre-seeded to 0. `zero_mode=True` matches Huawei 0-based addressing.
- **`RegisterBank`** — wraps the server context; `update(pv_w, batt_w, soc_pct, grid_w)` writes to both Huawei and SunSpec registers; skips `None` (keeps previous value).
- **`HAClient`** — async REST wrapper around HA Supervisor API. `get_state(entity_id)` reads sensors.
- **`SurplusState`** — dataclass tracking HW comfort mode, heating offset, and hysteresis counter.
- **`NibeHuaweiBridge`** — orchestrator; reads HA sensors each cycle and calls `bank.update()`.
- **`async_main(server, bridge)`** — runs Modbus TCP server and bridge loop concurrently via `asyncio.gather`.

### Two modes (can run simultaneously)

**Mode 1 – Modbus TCP Server (default, enabled):** Embedded pymodbus TCP slave on port 5020 emulating SUN2000. Nibe polls it directly. Both Huawei proprietary and SunSpec model 103 registers are always populated.

**Mode 2 – Surplus Control:** Derives surplus from grid power (`surplus = -grid_power`), then uses a threshold state machine to set HW comfort mode (reg 47041) and heating offset (reg 47276). Hysteresis (`below_count`) prevents oscillation. Writes via HA `modbus.write_register` — requires a separate HA Modbus hub.

### Signed 16-bit Modbus values

Negative values (grid export, battery discharge) use two's complement: `value & 0xFFFF`.

### config.yaml / addon UI

Every field in the `schema:` section renders as an editable input in the HA addon UI, pre-filled with `options:` defaults. Nested objects (e.g. `sensors`, `modbus_server`) render as grouped fields.

### Port note

Port 502 requires root. Default is 5020 — no privileges needed. Change `modbus_server.port` and the `ports:` mapping in `config.yaml` together.

### Config

All parameters come from `/data/options.json` (written by HA from the addon UI). Schema is defined in `config.yaml`. Sensor entity IDs, thresholds, and Modbus server settings are all user-configurable.

## HA Entity IDs (this homelab)

These are the actual entity IDs from the Huawei EMMA integration (see `~/git/homelab/docs/home-assistant.md`):

| Config key | Entity ID | Description |
|---|---|---|
| `sensors.pv_power` | `sensor.inverter_input_power` | PV DC input power (W) — use inverter entity, not EMMA; `emma_pv_output_power` returns 0 |
| `sensors.battery_soc` | `sensor.emma_state_of_capacity` | Battery SoC (%) |
| `sensors.battery_power` | `sensor.emma_battery_charge_discharge_power` | Battery power (W) |
| `sensors.grid_power` | `sensor.emma_feed_in_power` | Grid power (W, **positive = export**) |

Note on sign convention: `emma_feed_in_power` is positive when exporting to grid and negative when importing — which matches what the bridge expects (`surplus = -grid_power`).

The Nibe uses the `nibe_heatpump` HA integration (UI-managed, not a YAML modbus block). The `nibe_hub` surplus_control config value must match whatever Modbus hub name is defined in HA's `configuration.yaml` for direct register writes.

## Key Registers

### Huawei SUN2000 (emulated by the bridge)

The S1255 acts as a Modbus TCP **master** in this architecture — it connects to the bridge (port 5020) and polls for data. The bridge is the slave/server.

| Address | Type | Description |
|---|---|---|
| 30071 | UINT16 | PV power — MBSA V1 (W) |
| 30073 | UINT16 | Rated power (kW) |
| 32008 | INT16×10 | PV string 1 voltage (V) |
| 32010 | INT16×10 | PV string 2 voltage (V) |
| 32080–32081 | INT32 | PV power (W) |
| 37000 | UINT16 | Storage running status (2=running) |
| 37001–37002 | INT32 | Storage unit 1 power (W) |
| 37004 | UINT16 | Storage unit 1 SoC (% × 10) |
| 37100 | UINT16 | Meter status (1=normal) |
| 37101–37102 | INT32 | Load/consumption power (W) |
| 37113–37114 | INT32 | Grid power (W, +export / −import) |
| 37758 | UINT16 | Max charge power (W) |
| 37759 | UINT16 | Max discharge power (W) |
| 37760 | UINT16 | Battery SoC (% × 10) |
| 37762 | UINT16 | Battery type (1=LUNA2000) |
| 37765–37766 | INT32 | Battery power (W, +charge / −discharge) |
| 40000–40001 | — | SunSpec "SunS" identifier |
| 40070– | — | SunSpec Model 103 (Three-phase inverter) |

### Nibe S1255 (surplus control mode only)

| Register | Purpose | Values |
|---|---|---|
| 47041 | HW comfort mode | 0=ECO, 1=Normal, 2=Luxury |
| 47276 | Heating offset climate system 1 | -10 to +10 |
