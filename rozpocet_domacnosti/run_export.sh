#!/bin/sh

EXPORT_DIR="/share/haneva-budget-migration"
TMP_ARCHIVE="$EXPORT_DIR/budget-data.tgz.tmp"
FINAL_ARCHIVE="$EXPORT_DIR/budget-data.tgz"

mkdir -p "$EXPORT_DIR"
rm -f "$TMP_ARCHIVE"

# Export probíhá ještě před spuštěním aplikace, takže SQLite není otevřená
# a snapshot persistentního /data je konzistentní.
if tar czf "$TMP_ARCHIVE" -C /data .; then
  mv -f "$TMP_ARCHIVE" "$FINAL_ARCHIVE"
  date -Iseconds > "$EXPORT_DIR/exported-at.txt" 2>/dev/null || echo exported > "$EXPORT_DIR/exported-at.txt"
  echo "Rozpočet domácnosti: migrační snapshot pro Haneva Home byl vytvořen."
else
  rm -f "$TMP_ARCHIVE"
  echo "Rozpočet domácnosti: migrační snapshot se nepodařilo vytvořit, aplikace se spustí beze změny."
fi

exec python3 /app/app.py
