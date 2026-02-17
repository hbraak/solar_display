#!/bin/bash
# Holt Sonnenstunden-Forecast von Open-Meteo (Köln) für heute, morgen, übermorgen.
# Schreibt in ~/.sonneheute, ~/.sonnemorgen, ~/.sonneuebermorgen, ~/.datum

API="https://api.open-meteo.com/v1/forecast?latitude=50.845&longitude=7.483&daily=sunshine_duration&timezone=Europe/Berlin&forecast_days=3"

JSON=$(curl -sf "$API" 2>/dev/null)
if [ -z "$JSON" ]; then
    echo "API-Fehler" >&2
    exit 1
fi

# Sekunden → Stunden (1 Dezimalstelle), mit awk (kein jq nötig)
HEUTE=$(echo "$JSON" | grep -oP '"sunshine_duration":\[\K[0-9.]+' | head -1)
MORGEN=$(echo "$JSON" | grep -oP '"sunshine_duration":\[[0-9.]+,\K[0-9.]+')
UEBERM=$(echo "$JSON" | grep -oP '"sunshine_duration":\[[0-9.]+,[0-9.]+,\K[0-9.]+')

to_hours() {
    awk "BEGIN {printf \"%.1f\", $1 / 3600}"
}

echo "$(to_hours "${HEUTE:-0}")" > ~/.sonneheute
echo "$(to_hours "${MORGEN:-0}")" > ~/.sonnemorgen
echo "$(to_hours "${UEBERM:-0}")" > ~/.sonneuebermorgen
echo "$(date '+%d.%m. %H:%M')" > ~/.datum
