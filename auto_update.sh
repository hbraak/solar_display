#!/bin/bash
# Auto-Update für solar_display vom GitHub-Repo.
# Läuft per Cron (z.B. 1:00 nachts), prüft auf Änderungen, zieht Updates.

REPO_DIR="/home/pi/solar_display"
LOG="/tmp/solar_display_update.log"
SERVICE="solar-display"

cd "$REPO_DIR" || exit 1

echo "$(date): Update-Check gestartet" >> "$LOG"

# Fetch ohne zu mergen
git fetch origin main 2>>"$LOG"

# Vergleiche lokalen und remote HEAD
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "$(date): Kein Update verfügbar." >> "$LOG"
    exit 0
fi

echo "$(date): Update gefunden! $LOCAL → $REMOTE" >> "$LOG"

# Pull
git pull origin main 2>>"$LOG"

# Service neustarten
sudo systemctl restart "$SERVICE" 2>>"$LOG"

echo "$(date): Update installiert, Service neugestartet." >> "$LOG"
