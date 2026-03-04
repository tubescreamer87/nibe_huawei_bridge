# Nibe-Huawei Bridge – HA Addon

Addon pre Home Assistant ktorý prenáša dáta z Huawei FV invertera a batérie
do tepelného čerpadla Nibe S1255 cez existujúcu Modbus integráciu v HA.

## Architektúra

```
Huawei Inverter/Batéria
       │ (Modbus / SunSpec)
       ▼
 Home Assistant          ← addon číta stavy cez Supervisor REST API
       │
       │ (HA Modbus service call)
       ▼
  Nibe MODBUS40
       │
       ▼
  Nibe S1255 regulácia
```

Addon nekomunikuje priamo s Nibe – všetko ide cez HA `modbus.write_register`
službu, takže využíva existujúce spojenie a konfiguráciu.

---

## Inštalácia

1. Skopíruj celý priečinok `nibe_huawei_bridge` do `/config/addons/` na tvojom HA
2. V HA: **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**
3. Addon sa objaví pod "Local add-ons" – nainštaluj ho
4. Nakonfiguruj (pozri nižšie) a spusti

---

## Konfigurácia

### Povinné – zisti názvy entít

V HA Developer Tools → States si nájdi presné mená entít:

| Parameter | Popis | Príklad |
|-----------|-------|---------|
| `sensors.pv_power` | Výkon FV panelov (W) | `sensor.huawei_solar_inverter_power` |
| `sensors.battery_soc` | Stav nabitia batérie (%) | `sensor.huawei_battery_state_of_capacity` |
| `sensors.battery_power` | Výkon batérie (W, +nabíjanie/-vybíjanie) | `sensor.huawei_battery_charge_discharge_power` |
| `sensors.grid_power` | Výkon siete (W, +import/-export) | `sensor.huawei_power_grid_total_power` |

### Povinné – názov Nibe Modbus hubu

V `configuration.yaml` nájdi svoju Modbus konfiguráciu:
```yaml
modbus:
  - name: nibe        # ← toto je "nibe_hub" v nastaveniach addonu
    type: tcp
    host: 192.168.x.x
    port: 502
```

### Režim 1: Surplus control (odporúčaný štart)

```yaml
surplus_control:
  enabled: true
  tuv_normal_w: 1000      # od 1kW prebytku → TÚV Normal
  tuv_luxury_w: 3000      # od 3kW prebytku → TÚV Luxury (max teplota)
  heating_offset_w: 5000  # od 5kW prebytku → zvýš heating o offset
  heating_offset_value: 3 # °C offset (pozitívny = teplejšie)
  hysteresis_cycles: 3    # počet cyklov pod prahom pred znížením
```

Toto je bezpečný režim – addon nastavuje len HW comfort mode a heating offset,
Nibe zvyšok riadi sama podľa svojej logiky.

### Režim 2: Externé energetické registre (pokročilý)

```yaml
nibe_external_registers:
  enabled: true
  pv_power: 43086      # ⚠️ OVERTE v MODBUS40 dokumentácii!
  battery_power: 43087
  battery_soc: 43088
  grid_power: 43084
```

**POZOR:** Tieto čísla registrov sa líšia podľa verzie firmvéru MODBUS40.
Pred zapnutím tohto režimu:
1. Stiahni "Nibe MODBUS40 Modbus Manager" dokumentáciu pre S1255
2. Hľadaj sekciu "External energy meter" alebo "PV system"
3. Uprav čísla registrov podľa tvojho firmvéru

Oba režimy môžu bežať súčasne – externé registre nechajú Nibe riadiť sa
sama, surplus control je záložný/doplnkový mechanizmus.

---

## Nibe MODBUS40 registre (S1255)

| Register | Popis | Hodnoty |
|----------|-------|---------|
| `47041` | HW comfort mode | 0=ECO, 1=Normal, 2=Luxury |
| `47276` | Heating offset | -10 až +10 |
| `43084` | Grid power* | W (signed) |
| `43086` | PV power* | W |
| `43087` | Battery power* | W (signed) |
| `43088` | Battery SOC* | % |

*) Overiť v MODBUS40 dokumentácii pre konkrétny firmware

---

## Troubleshooting

**Addon štartuje ale nepíše do Nibe:**
- Skontroluj logy (log_level: debug)
- Over, že `nibe_hub` zodpovedá názvu v `configuration.yaml`
- Skontroluj v HA Developer Tools → Services či `modbus.write_register` funguje manuálne

**Hodnoty sú unavailable:**
- Skontroluj názvy entít v Developer Tools → States
- Huawei inverter musí byť online a HA integrácia funkčná

**Nibe nereaguje na zmeny HW mode:**
- Skontroluj register 47041 v Nibe MODBUS40 dokumentácii
- Over, že Modbus povolenia umožňujú zápis (nie len čítanie)
- Niektoré Nibe verzie vyžadujú aktiváciu "External control" v menu
