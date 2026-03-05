# Nibe-Huawei Bridge – HA Addon

Emulates a Huawei SUN2000 inverter over Modbus TCP. Nibe S1255 connects directly to the bridge as a Modbus master and polls PV, battery, and grid data — no HA Modbus hub required.

```
Huawei EMMA / Inverter / Battery
       │
       ▼
 Home Assistant  ← bridge reads sensor states via Supervisor REST API
       │
       ▼
 nibe_huawei_bridge (this addon)
       │  Modbus TCP :5020  (emulates SUN2000)
       ▼
 Nibe S1255  ← connects as Modbus master, reads PV/battery/grid registers
```

---

## Installation

### 1. Add the repository as a local addon

Copy (or clone) this repo into your HA config folder:

```bash
# On your HA host (SSH or terminal addon)
cd /config/addons
git clone https://github.com/tubescreamer87/nibe_huawei_bridge.git
```

Or copy the folder manually via Samba/SFTP — the result must be:

```
/config/addons/nibe_huawei_bridge/
  bridge.py
  config.yaml
  Dockerfile
  run.sh
  build.yaml
```

### 2. Install in HA

1. **Settings → Add-ons → Add-on Store → ⋮ menu → Check for updates**
2. The addon appears under **Local add-ons** — click it and press **Install**
3. HA builds the Docker image (takes ~1–2 min the first time)

### 3. Configure the addon

Open the addon's **Configuration** tab. The defaults match the Huawei EMMA integration entity IDs — adjust if yours differ:

| Field | Default | Description |
|---|---|---|
| `sensors.pv_power` | `sensor.emma_pv_output_power` | PV production (W) |
| `sensors.battery_soc` | `sensor.emma_state_of_capacity` | Battery SoC (%) |
| `sensors.battery_power` | `sensor.emma_battery_charge_discharge_power` | Battery power (W) |
| `sensors.grid_power` | `sensor.emma_feed_in_power` | Grid power (W, positive = export) |
| `modbus_server.port` | `5020` | Port the bridge listens on |
| `modbus_server.unit_id` | `1` | Modbus slave/unit ID |
| `update_interval` | `30` | How often to poll HA sensors (seconds) |

To find your exact entity IDs: **Developer Tools → States**, filter by `emma` or `huawei`.

### 4. Start the addon

Press **Start**. Check the **Log** tab — you should see:

```
Nibe-Huawei Bridge štartuje
  Modbus server:      zapnutý
Modbus TCP server na 0.0.0.0:5020 (unit_id=1)
```

### 5. Configure Nibe S1255

In the Nibe S1255 service menu, configure the external inverter connection:

1. Navigate to **Service** → **Energy** (or similar — exact path depends on firmware)
2. Set **External inverter IP** to the IP address of your HA host
3. Set **Port** to `5020` (or whatever you set in `modbus_server.port`)
4. Set **Unit ID** / **Slave ID** to `1`
5. Save and restart the Nibe controller if prompted

The Nibe will now poll the bridge every few seconds. Confirm it's working via **Nibe service menu → External energy status** — it should show non-zero PV/battery values when the sun is shining.

You can also verify from the HA side: the `nibe_heatpump` integration should start reporting values in entities like `sensor.pv_available_power`.

### 6. Expose port 5020 (if needed)

The addon maps port 5020 on the container to port 5020 on the host by default (configured in `config.yaml`). If Nibe cannot reach HA on port 5020, check:

- HA host firewall allows TCP 5020 inbound
- Nibe and HA are on the same VLAN / can route to each other
- The addon's **Network** tab in HA shows port 5020 mapped

---

## Surplus control (optional fallback)

If you also want the bridge to directly set Nibe's HW comfort mode and heating offset based on surplus power, enable it under `surplus_control`:

```yaml
surplus_control:
  enabled: true
  nibe_hub: "nibe"          # must match your HA modbus hub name
  tuv_normal_w: 1000        # ≥1 kW surplus → HW Normal
  tuv_luxury_w: 3000        # ≥3 kW surplus → HW Luxury
  heating_offset_w: 5000    # ≥5 kW surplus → raise heating offset
  heating_offset_value: 3   # degrees to add
  hysteresis_cycles: 3      # cycles below threshold before stepping down
```

This writes to Nibe via HA's `modbus.write_register` and requires an HA Modbus hub pointed at the Nibe. It can run alongside the Modbus server.

---

## Register reference

### Huawei SUN2000 registers emulated by the bridge

| Address | Type | Description |
|---|---|---|
| 32080–32081 | INT32 | PV power (W) |
| 37113–37114 | INT32 | Grid power (W, +export / −import) |
| 37760 | UINT16 | Battery SoC (% × 10) |
| 37765–37766 | INT32 | Battery power (W, +charge / −discharge) |
| 40000–40001 | — | SunSpec "SunS" identifier |
| 40002– | — | SunSpec Model 1 (Common) |
| 40070– | — | SunSpec Model 103 (Three-phase inverter) |

### Nibe S1255 registers (surplus control mode only)

| Address | Description | Values |
|---|---|---|
| 47041 | HW comfort mode | 0=ECO, 1=Normal, 2=Luxury |
| 47276 | Heating offset climate system 1 | −10 to +10 |

---

## Troubleshooting

**Nibe does not connect / shows no external inverter data**
- Confirm the HA host IP and port 5020 are reachable from the Nibe network segment
- Check addon logs for `Modbus TCP server na 0.0.0.0:5020` — if missing, the server failed to start
- Try polling the bridge manually from another machine: `mbpoll -a 1 -r 32080 -c 2 <HA_IP> -p 5020`

**All register values are 0**
- The bridge only writes values after the first successful HA sensor read
- Check that the sensor entity IDs in the addon config match what's in **Developer Tools → States**
- Set `log_level: debug` to see each sensor read and register update

**PermissionError on startup**
- Port 502 requires root; the default port 5020 does not — keep the default unless you have a specific reason to use 502

**Sensor shows `unavailable`**
- The Huawei EMMA / inverter integration must be online and returning values
- The bridge skips `None` reads and keeps the previous register value, so a brief outage won't zero out Nibe's view

**Surplus control not writing to Nibe**
- Verify `nibe_hub` matches the hub name in `configuration.yaml`
- Test manually: **Developer Tools → Services → modbus.write_register**
- Some Nibe firmware versions require enabling "External control" in the service menu before register writes are accepted
