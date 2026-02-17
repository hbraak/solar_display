#!/bin/bash
# Solar Display Setup Script für Raspberry Pi 2
# Ausführen nach erstem Boot: sudo bash setup.sh

set -e

echo "=== Solar Display Setup ==="

# System-Updates
echo "[1/6] System aktualisieren..."
sudo apt update && sudo apt upgrade -y

# System-Pakete für I2C und Python
echo "[2/6] System-Pakete installieren..."
sudo apt install -y python3-pip python3-venv i2c-tools python3-smbus python3-dev libopenjp2-7 libtiff6

# I2C testen
echo "[3/6] I2C prüfen..."
if i2cdetect -y 1 | grep -q "3c"; then
    echo "  ✓ OLED Display gefunden auf 0x3C"
else
    echo "  ⚠ OLED Display NICHT gefunden! Verkabelung prüfen!"
fi

# Python venv + Dependencies
echo "[4/6] Python-Umgebung einrichten..."
cd /home/pi/solar_display
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Font herunterladen (Code2000 wie im Original)
echo "[5/6] Font herunterladen..."
if [ ! -f fonts/code2000.ttf ]; then
    # Fallback: DejaVu (ist auf Raspbian vorinstalliert)
    cp /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf fonts/code2000.ttf 2>/dev/null || \
    echo "  ⚠ Font nicht gefunden - bitte code2000.ttf manuell nach fonts/ kopieren"
fi

# Systemd Service
echo "[6/6] Systemd Service einrichten..."
sudo tee /etc/systemd/system/solar-display.service << 'SERVICE'
[Unit]
Description=Solar OLED Display (Cerbo GX)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/solar_display
ExecStart=/home/pi/solar_display/venv/bin/python3 cerbo_display.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable solar-display.service

echo ""
echo "=== Setup abgeschlossen! ==="
echo ""
echo "Nächste Schritte:"
echo "1. /etc/hosts prüfen: IP von 'einstein' (Cerbo GX) anpassen"
echo "2. OLED-Verkabelung prüfen (SDA→GPIO2, SCL→GPIO3, VCC→3.3V, GND)"
echo "3. Schalter verkabeln (GPIO17, GPIO27, GPIO24)"
echo "4. Test: sudo systemctl start solar-display"
echo "5. Logs: journalctl -u solar-display -f"
