#!/bin/sh

MIGRATION_DIR="/share/haneva-budget-migration"
MIGRATION_ARCHIVE="$MIGRATION_DIR/budget-data.tgz"
MIGRATION_MARKER="/data/.haneva_budget_migrated"
TMP_DIR="/tmp/haneva-budget-import"
BUDGET_MODE="external"

# Jednorázově převezmeme persistentní /data ze starého add-onu.
# Haneva vlastní DB a options.json se nikdy nepřepisují.
if [ -f "$MIGRATION_MARKER" ]; then
  BUDGET_MODE="embedded"
elif [ -f "$MIGRATION_ARCHIVE" ]; then
  echo "Haneva: nalezen export původního rozpočtu, spouštím migraci."
  rm -rf "$TMP_DIR"
  mkdir -p "$TMP_DIR"
  if tar xzf "$MIGRATION_ARCHIVE" -C "$TMP_DIR"; then
    for entry in "$TMP_DIR"/* "$TMP_DIR"/.[!.]* "$TMP_DIR"/..?*; do
      [ -e "$entry" ] || continue
      name="$(basename "$entry")"
      case "$name" in
        options.json|calendar.db|shopping.db|haneva_profiles.db|.haneva_budget_migrated)
          continue
          ;;
      esac
      rm -rf "/data/$name"
      cp -a "$entry" /data/
    done
    date -Iseconds > "$MIGRATION_MARKER" 2>/dev/null || echo migrated > "$MIGRATION_MARKER"
    BUDGET_MODE="embedded"
    echo "Haneva: data rozpočtu byla převedena do Haneva Home."
  else
    echo "Haneva: export rozpočtu se nepodařilo rozbalit, dočasně používám původní add-on."
  fi
  rm -rf "$TMP_DIR"
fi

python3 /app/seed_holidays.py

if [ "$BUDGET_MODE" = "embedded" ]; then
  echo "Haneva: spouštím vestavěný Rozpočet domácnosti na 127.0.0.1:8099."
  python3 /app/budget/app.py &
  export HANEVA_BUDGET_HOST="127.0.0.1"
  export HANEVA_BUDGET_PORT="8099"
  sleep 1
else
  echo "Haneva: migrační export zatím není k dispozici, rozpočet zůstává napojený na původní add-on."
  export HANEVA_BUDGET_HOST="192.168.0.60"
  export HANEVA_BUDGET_PORT="8099"
fi

exec python3 /app/consolidated_gateway.py
