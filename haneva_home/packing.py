from datetime import date
from pathlib import Path
import sqlite3


DB_PATH = "/data/haneva_v1.db"
OWNERS = {"shared": "Společné", "hanych": "Hanych", "eva": "Eva"}
CATEGORIES = (
    "Oblečení", "Elektronika", "Hygiena", "Dokumenty", "Zdraví",
    "Jídlo a pití", "Na cestu", "Vybavení", "Ostatní",
)
ESSENTIALS = (
    ("Ponožky", "Oblečení", "person", "per_day", 1),
    ("Spodní prádlo", "Oblečení", "person", "per_day", 1),
    ("Nabíječka", "Elektronika", "person", "fixed", 1),
    ("Kartáček na zuby", "Hygiena", "person", "fixed", 1),
    ("Doklady", "Dokumenty", "person", "fixed", 1),
    ("Lékárnička", "Zdraví", "shared", "fixed", 1),
    ("Klíče", "Na cestu", "shared", "fixed", 1),
)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS packing_trips(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          start_date TEXT NOT NULL,
          end_date TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS packing_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          trip_id INTEGER NOT NULL REFERENCES packing_trips(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          owner TEXT NOT NULL CHECK(owner IN ('shared','hanych','eva')),
          category TEXT NOT NULL DEFAULT 'Ostatní',
          quantity_value INTEGER NOT NULL DEFAULT 1,
          quantity_mode TEXT NOT NULL DEFAULT 'fixed' CHECK(quantity_mode IN ('fixed','per_day')),
          packed INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_packing_items_trip ON packing_items(trip_id,packed,owner,category,id);
        """)
        conn.commit()


def _clean(value, limit):
    return " ".join(str(value or "").strip().split())[:limit]


def _valid_date(value):
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("Vyber platné datum.") from exc


def _duration(start_date, end_date):
    return max(1, (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1)


def _trip_dict(row, items=()):
    result = dict(row)
    result["duration_days"] = _duration(result["start_date"], result["end_date"])
    result["items"] = [_item_dict(item, result["duration_days"]) for item in items]
    result["item_count"] = len(result["items"])
    result["packed_count"] = sum(1 for item in result["items"] if item["packed"])
    result["progress"] = round(result["packed_count"] / result["item_count"] * 100) if result["item_count"] else 0
    return result


def _item_dict(row, duration_days):
    result = dict(row)
    result["packed"] = bool(result["packed"])
    result["owner_name"] = OWNERS[result["owner"]]
    result["quantity"] = result["quantity_value"] * duration_days if result["quantity_mode"] == "per_day" else result["quantity_value"]
    return result


def state():
    with db() as conn:
        trips = conn.execute(
            "SELECT * FROM packing_trips ORDER BY CASE WHEN end_date>=date('now','localtime') THEN 0 ELSE 1 END,start_date DESC,id DESC"
        ).fetchall()
        item_rows = conn.execute("SELECT * FROM packing_items ORDER BY packed,owner,category,id").fetchall()
    by_trip = {}
    for item in item_rows:
        by_trip.setdefault(item["trip_id"], []).append(item)
    return {
        "trips": [_trip_dict(trip, by_trip.get(trip["id"], ())) for trip in trips],
        "owners": [{"key": key, "name": name} for key, name in OWNERS.items()],
        "categories": list(CATEGORIES),
    }


def save_trip(payload, trip_id=None):
    if not isinstance(payload, dict):
        raise ValueError("Seznam má neplatný formát.")
    current = None
    if trip_id is not None:
        with db() as conn:
            current = conn.execute("SELECT * FROM packing_trips WHERE id=?", (int(trip_id),)).fetchone()
        if not current:
            raise KeyError("Balicí seznam nebyl nalezen.")
        current = dict(current)

    def value(key, default=""):
        return payload[key] if key in payload else (current.get(key, default) if current else default)

    name = _clean(value("name"), 140)
    if not name:
        raise ValueError("Napiš název cesty nebo pobytu.")
    start = _valid_date(value("start_date", date.today().isoformat()))
    end = _valid_date(value("end_date", start.isoformat()))
    if end < start:
        raise ValueError("Konec pobytu nemůže být před začátkem.")
    if (end - start).days > 365:
        raise ValueError("Pobyt může mít nejvýše 366 dní.")
    note = _clean(value("note"), 500)
    with db() as conn:
        if current:
            conn.execute(
                "UPDATE packing_trips SET name=?,start_date=?,end_date=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, start.isoformat(), end.isoformat(), note, int(trip_id)),
            )
            saved_id = int(trip_id)
        else:
            saved_id = conn.execute(
                "INSERT INTO packing_trips(name,start_date,end_date,note) VALUES(?,?,?,?)",
                (name, start.isoformat(), end.isoformat(), note),
            ).lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM packing_trips WHERE id=?", (saved_id,)).fetchone()
        items = conn.execute("SELECT * FROM packing_items WHERE trip_id=? ORDER BY packed,owner,category,id", (saved_id,)).fetchall()
    saved = _trip_dict(row, items)
    if not current:
        # Každá nová cesta začíná bezpečným základem. Uživatel může položky
        # libovolně upravit nebo smazat a tlačítkem je později znovu doplnit.
        add_essentials(saved_id)
        return next(trip for trip in state()["trips"] if trip["id"] == saved_id)
    return saved


def delete_trip(trip_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM packing_trips WHERE id=?", (int(trip_id),))
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Balicí seznam nebyl nalezen.")


def save_item(payload, trip_id=None, item_id=None):
    if not isinstance(payload, dict):
        raise ValueError("Položka má neplatný formát.")
    current = None
    if item_id is not None:
        with db() as conn:
            current = conn.execute("SELECT * FROM packing_items WHERE id=?", (int(item_id),)).fetchone()
        if not current:
            raise KeyError("Položka nebyla nalezena.")
        current = dict(current)
        trip_id = current["trip_id"]
    if trip_id is None:
        raise ValueError("Chybí balicí seznam.")

    def value(key, default=""):
        return payload[key] if key in payload else (current.get(key, default) if current else default)

    name = _clean(value("name"), 140)
    if not name:
        raise ValueError("Napiš, co chcete zabalit.")
    owner = str(value("owner", "shared") or "").strip().lower()
    if owner not in OWNERS:
        raise ValueError("Vyber, komu věc patří.")
    category = _clean(value("category", "Ostatní"), 80)
    if category not in CATEGORIES:
        raise ValueError("Vyber kategorii ze seznamu.")
    mode = str(value("quantity_mode", "fixed") or "").strip().lower()
    if mode not in {"fixed", "per_day"}:
        raise ValueError("Vyber platný způsob výpočtu množství.")
    try:
        quantity_value = int(value("quantity_value", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("Množství musí být celé číslo.") from exc
    if not 1 <= quantity_value <= 99:
        raise ValueError("Množství musí být od 1 do 99.")
    packed = 1 if bool(value("packed", False)) else 0
    with db() as conn:
        if not conn.execute("SELECT 1 FROM packing_trips WHERE id=?", (int(trip_id),)).fetchone():
            raise KeyError("Balicí seznam nebyl nalezen.")
        if current:
            conn.execute(
                """UPDATE packing_items SET name=?,owner=?,category=?,quantity_value=?,quantity_mode=?,packed=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (name, owner, category, quantity_value, mode, packed, int(item_id)),
            )
            saved_id = int(item_id)
        else:
            saved_id = conn.execute(
                "INSERT INTO packing_items(trip_id,name,owner,category,quantity_value,quantity_mode,packed) VALUES(?,?,?,?,?,?,?)",
                (int(trip_id), name, owner, category, quantity_value, mode, packed),
            ).lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM packing_items WHERE id=?", (saved_id,)).fetchone()
        trip = conn.execute("SELECT start_date,end_date FROM packing_trips WHERE id=?", (int(trip_id),)).fetchone()
    return _item_dict(row, _duration(trip["start_date"], trip["end_date"]))


def delete_item(item_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM packing_items WHERE id=?", (int(item_id),))
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Položka nebyla nalezena.")


def add_essentials(trip_id):
    trip_id = int(trip_id)
    added = 0
    with db() as conn:
        if not conn.execute("SELECT 1 FROM packing_trips WHERE id=?", (trip_id,)).fetchone():
            raise KeyError("Balicí seznam nebyl nalezen.")
        existing = {
            (row["name"].casefold(), row["owner"])
            for row in conn.execute("SELECT name,owner FROM packing_items WHERE trip_id=?", (trip_id,)).fetchall()
        }
        for name, category, assignment, mode, quantity in ESSENTIALS:
            owners = ("hanych", "eva") if assignment == "person" else ("shared",)
            for owner in owners:
                if (name.casefold(), owner) in existing:
                    continue
                conn.execute(
                    "INSERT INTO packing_items(trip_id,name,owner,category,quantity_value,quantity_mode) VALUES(?,?,?,?,?,?)",
                    (trip_id, name, owner, category, quantity, mode),
                )
                existing.add((name.casefold(), owner))
                added += 1
        conn.commit()
    return {"added": added, **state()}
