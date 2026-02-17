# Solar Display — Cerbo GX OLED Controller

Raspberry Pi 1 Model B mit SH1106 OLED-Display (128×64, I2C) zeigt Victron-Solardaten vom Cerbo GX via Modbus TCP an.

## Features

- **4 Screens:** Übersicht (PV/SOC/Batterie/MP/Generator), PV-Detail + Ertrag, Batterie (SoC/Status/AC-Last/SoH), Sonnenstunden (heute/morgen/übermorgen)
- **2 Kippschalter** (Generator / Multiplus II) mit 5s-Taster-Bestätigung
- **Taster** für Screen-Wechsel, Auto-Reset nach 10 Ticks
- Persistente Modbus-Verbindung mit Auto-Reconnect + Watchdog
- **Sonnenstunden-Forecast** via Open-Meteo API (Standort: Ruppichteroth)
- **Auto-Update** vom GitHub-Repo (nächtlicher Cron-Job)

## Hardware

| Komponente | Pin/Adresse |
|---|---|
| SH1106 OLED | I2C Port 1, Adresse 0x3C |
| Kippschalter Generator | GPIO 17 (BCM) |
| Kippschalter Multiplus | GPIO 27 (BCM) |
| Taster Screen-Wechsel | GPIO 24 (BCM) |
| Cerbo GX | Hostname `einstein`, Port 502 |

## Setup

```bash
chmod +x setup.sh
./setup.sh
```

Oder manuell:
```bash
sudo apt install python3-pip python3-venv i2c-tools
sudo raspi-config  # I2C aktivieren
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Systemd Service

```bash
sudo cp solar-display.service /etc/systemd/system/
sudo systemctl enable solar-display
sudo systemctl start solar-display
```

## Auto-Update

Der Pi prüft jede Nacht um 1:00 auf Updates:
```bash
chmod +x auto_update.sh
# Cron-Eintrag (wird von setup.sh angelegt):
# 0 1 * * * /home/pi/solar_display/auto_update.sh
```

Log: `/tmp/solar_display_update.log`

## Sonnenstunden

`update_sunshine.sh` holt Forecast von Open-Meteo (kostenlos, kein API-Key).
Läuft per Cron alle 2 Stunden.

## Font

Display nutzt **PixelOperator Bold** — optimiert für kleine OLED-Displays.

## Anforderungen

- Raspberry Pi (getestet auf Pi 1 Model B, ARMv6)
- Python 3.10+
- pymodbus 3.x, gpiozero, lgpio, luma.oled, Pillow
