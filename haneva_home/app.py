from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
import shutil
import sys
import threading

import shopping

HOST = "0.0.0.0"
PORT = 8100
DB_PATH = "/data/calendar.db"
TICKET_DIR = "/data/calendar_tickets"
MAX_TICKET_SIZE = 15 * 1024 * 1024
HOME_HTML_PATH = "/app/home.html"
CALENDAR_HTML_PATH = "/app/calendar.html"
SHOPPING_HTML_PATH = "/app/shopping.html"
ALLOWED_CALENDARS = {"hanych", "eva", "spolecne", "narozeniny", "kumi"}
COLOR_KEYS = ALLOWED_CALENDARS | {"svatky"}
HOLIDAY_NOTE = "Automaticky přidaný den pracovního klidu v ČR."
DEFAULT_COLORS = {
    "hanych": "#60a5fa",
    "eva": "#c084fc",
    "spolecne": "#34d399",
    "narozeniny": "#fb923c",
    "kumi": "#6b7280",
    "svatky": "#ef4444",
}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TICKET_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
TICKET_SCAN_LOCK = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                calendar TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                start_time TEXT,
                end_time TEXT,
                all_day INTEGER NOT NULL DEFAULT 1,
                location TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                recurrence TEXT NOT NULL DEFAULT 'none',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_dates ON events(start_date, end_date)")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "event_type" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN event_type TEXT NOT NULL DEFAULT ''")
        ticket_columns = {row["name"] for row in conn.execute("PRAGMA table_info(event_tickets)").fetchall()}
        if ticket_columns and "owner" not in ticket_columns:
            conn.execute("ALTER TABLE event_tickets RENAME TO event_tickets_legacy")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS event_tickets (
                event_id INTEGER NOT NULL,
                owner TEXT NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                qr_name TEXT NOT NULL DEFAULT '',
                qr_value TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(event_id, owner)
            )
        ''')
        if ticket_columns and "owner" not in ticket_columns:
            conn.execute('''INSERT INTO event_tickets
                (event_id, owner, original_name, stored_name, mime_type, size, qr_name, created_at, updated_at)
                SELECT event_id, 'hanych', original_name, stored_name, mime_type, size, qr_name, created_at, updated_at
                FROM event_tickets_legacy''')
            conn.execute("DROP TABLE event_tickets_legacy")
        current_ticket_columns = {row["name"] for row in conn.execute("PRAGMA table_info(event_tickets)").fetchall()}
        if "qr_value" not in current_ticket_columns:
            conn.execute("ALTER TABLE event_tickets ADD COLUMN qr_value TEXT NOT NULL DEFAULT ''")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    os.makedirs(TICKET_DIR, exist_ok=True)
    shopping.init_db()


def valid_date(value):
    try:
        return date.fromisoformat(value)
    except Exception:
        return None


def normalize_event(payload, existing=None):
    base = dict(existing) if existing else {}
    title = str(payload.get("title", base.get("title", ""))).strip()
    calendar = str(payload.get("calendar", base.get("calendar", "spolecne"))).strip()
    start_date = str(payload.get("start_date", base.get("start_date", ""))).strip()
    end_date = str(payload.get("end_date", base.get("end_date", start_date))).strip() or start_date
    all_day = bool(payload.get("all_day", bool(base.get("all_day", 1))))
    start_time = str(payload.get("start_time", base.get("start_time") or "")).strip()
    end_time = str(payload.get("end_time", base.get("end_time") or "")).strip()
    location = str(payload.get("location", base.get("location", ""))).strip()
    notes = str(payload.get("notes", base.get("notes", ""))).strip()
    recurrence = str(payload.get("recurrence", base.get("recurrence", "none"))).strip()
    event_type = str(payload.get("event_type", base.get("event_type", ""))).strip()

    if not title:
        raise ValueError("Název události je povinný.")
    if calendar not in ALLOWED_CALENDARS:
        raise ValueError("Neplatný kalendář.")
    sd = valid_date(start_date)
    ed = valid_date(end_date)
    if not sd or not ed:
        raise ValueError("Neplatné datum.")
    if ed < sd:
        raise ValueError("Konec události nemůže být před začátkem.")
    if recurrence not in {"none", "yearly"}:
        raise ValueError("Neplatné opakování.")
    if event_type not in {"", "zabava", "prace"}:
        raise ValueError("Neplatný typ události.")
    if all_day:
        start_time = ""
        end_time = ""

    return {
        "title": title[:200],
        "calendar": calendar,
        "start_date": start_date,
        "end_date": end_date,
        "start_time": start_time[:5],
        "end_time": end_time[:5],
        "all_day": 1 if all_day else 0,
        "location": location[:250],
        "notes": notes[:2000],
        "recurrence": recurrence,
        "event_type": event_type,
    }


def row_to_event(row):
    item = dict(row)
    item["all_day"] = bool(item["all_day"])
    item["source_start_date"] = item["start_date"]
    item["source_end_date"] = item["end_date"]
    item["system_kind"] = "holiday" if item.get("notes") == HOLIDAY_NOTE else ""
    item["has_ticket"] = bool(item.get("has_ticket", False))
    return item


def ticket_select_sql():
    return "e.*, EXISTS(SELECT 1 FROM event_tickets t WHERE t.event_id=e.id) AS has_ticket"


def ticket_metadata(row):
    if not row:
        return None
    event_id = int(row["event_id"])
    owner = row["owner"]
    return {
        "owner": owner,
        "name": row["original_name"],
        "mime_type": row["mime_type"],
        "size": row["size"],
        "original_url": f"/api/events/{event_id}/tickets/{owner}/file",
        "qr_url": f"/api/events/{event_id}/tickets/{owner}/qr" if row["qr_name"] else None,
    }


def get_ticket(event_id, owner):
    with db() as conn:
        return conn.execute("SELECT * FROM event_tickets WHERE event_id=? AND owner=?", (event_id, owner)).fetchone()


def get_tickets(event_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM event_tickets WHERE event_id=? ORDER BY CASE owner WHEN 'hanych' THEN 0 WHEN 'eva' THEN 1 ELSE CAST(owner AS INTEGER) END",
            (event_id,),
        ).fetchall()


def safe_ticket_path(name):
    if not name or Path(name).name != name:
        return None
    root = Path(TICKET_DIR).resolve()
    candidate = (root / name).resolve()
    return candidate if candidate.parent == root else None


def remove_ticket_files(row):
    if not row:
        return
    for key in ("stored_name", "qr_name"):
        # Shared original PDF must survive deletion of just one legacy slot.
        if row[key]:
            with db() as conn:
                if conn.execute("SELECT 1 FROM event_tickets WHERE stored_name=? OR qr_name=?",
                                (row[key], row[key])).fetchone():
                    continue
        path = safe_ticket_path(row[key])
        if path:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def detect_ticket_type(body):
    if body.startswith(b"%PDF-"):
        return "application/pdf"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Nahraj PDF nebo obrázek JPG, PNG či WebP.")


def save_ticket_bundle(event_id, original_name, body):
    if not body or len(body) > MAX_TICKET_SIZE:
        raise ValueError("Vyber soubor o velikosti nejvýše 15 MB.")
    mime_type = detect_ticket_type(body)
    with db() as conn:
        event = conn.execute("SELECT event_type FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        raise KeyError("Událost nebyla nalezena.")
    if event["event_type"] != "zabava":
        raise ValueError("Vstupenky lze přidat jen k události typu Zábava.")
    # Only one render at a time, important on Raspberry Pi.
    if not TICKET_SCAN_LOCK.acquire(blocking=False):
        raise ValueError("Právě zpracovávám jiné vstupenky. Zkus to za chvíli.")
    try:
        return _save_ticket_bundle(event_id, original_name, body, mime_type)
    finally:
        TICKET_SCAN_LOCK.release()


def _save_ticket_bundle(event_id, original_name, body, mime_type):
    clean_name = Path((original_name or "vstupenky").replace("\\", "/")).name.strip()[:180] or "vstupenky"
    token = uuid.uuid4().hex
    stored_name = token + TICKET_TYPES[mime_type]
    Path(TICKET_DIR).mkdir(parents=True, exist_ok=True)
    new_files = []
    try:
        with tempfile.TemporaryDirectory(prefix="haneva-bundle-") as temp:
            source = Path(temp) / ("source" + TICKET_TYPES[mime_type])
            source.write_bytes(body)
            try:
                proc = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("ticket_scan.py")), str(source), mime_type, temp],
                    capture_output=True, timeout=75,
                )
                result = json.loads(proc.stdout)
            except (OSError, subprocess.SubprocessError, ValueError):
                raise ValueError("Rozdělení vstupenek se nepodařilo dokončit. Zkus menší PDF nebo ostřejší obrázek.")
            if proc.returncode or result.get("error"):
                raise ValueError(result.get("error") or "QR kódy se nepodařilo načíst.")
            codes = result.get("codes", [])
            if not 1 <= len(codes) <= 50 or len({code["digest"] for code in codes}) != len(codes):
                raise ValueError("Soubor musí obsahovat čitelné, vzájemně odlišné QR kódy.")
            for code in codes:
                if Path(code["file"]).name != code["file"]:
                    raise ValueError("Neplatný výstup čtení vstupenek.")
            target = safe_ticket_path(stored_name)
            new_files.append(target)
            shutil.copyfile(source, target)
            rows = []
            for index, code in enumerate(codes, 1):
                owner = str(index)
                qr_name = f"{token}-{owner}.png"
                qr_path = safe_ticket_path(qr_name)
                new_files.append(qr_path)
                shutil.copyfile(Path(temp) / code["file"], qr_path)
                rows.append((event_id, owner, clean_name, stored_name, mime_type, len(body), qr_name, code["digest"]))
            # Original and all codes become visible together, never as a partial upload.
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                event = conn.execute("SELECT event_type FROM events WHERE id=?", (event_id,)).fetchone()
                if not event or event["event_type"] != "zabava":
                    raise ValueError("Událost byla mezitím změněna nebo odstraněna.")
                old = conn.execute("SELECT * FROM event_tickets WHERE event_id=?", (event_id,)).fetchall()
                conn.execute("DELETE FROM event_tickets WHERE event_id=?", (event_id,))
                conn.executemany(
                    """INSERT INTO event_tickets(event_id, owner, original_name, stored_name, mime_type, size, qr_name, qr_value)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?)""", rows)
                conn.commit()
    except Exception:
        for path in new_files:
            path.unlink(missing_ok=True)
        raise
    for row in old:
        remove_ticket_files(row)
    return [ticket_metadata(row) for row in get_tickets(event_id)]


def delete_ticket(event_id, owner=None):
    old = [get_ticket(event_id, owner)] if owner else get_tickets(event_id)
    old = [row for row in old if row]
    if not old:
        return False
    with db() as conn:
        if owner:
            conn.execute("DELETE FROM event_tickets WHERE event_id=? AND owner=?", (event_id, owner))
        else:
            conn.execute("DELETE FROM event_tickets WHERE event_id=?", (event_id,))
        conn.commit()
    for row in old:
        remove_ticket_files(row)
    return True


def events_for_range(start_s, end_s):
    start = valid_date(start_s)
    end = valid_date(end_s)
    if not start or not end or end < start:
        raise ValueError("Neplatný rozsah.")

    result = []
    with db() as conn:
        normal = conn.execute(
            f"SELECT {ticket_select_sql()} FROM events e WHERE recurrence='none' AND start_date <= ? AND end_date >= ? ORDER BY start_date, start_time",
            (end.isoformat(), start.isoformat()),
        ).fetchall()
        recurring = conn.execute(f"SELECT {ticket_select_sql()} FROM events e WHERE recurrence='yearly' ORDER BY start_date, start_time").fetchall()

    for row in normal:
        result.append(row_to_event(row))

    for row in recurring:
        base = row_to_event(row)
        base_start = date.fromisoformat(base["start_date"])
        base_end = date.fromisoformat(base["end_date"])
        duration = base_end - base_start
        for year in range(start.year - 1, end.year + 2):
            try:
                occ_start = date(year, base_start.month, base_start.day)
            except ValueError:
                continue
            occ_end = occ_start + duration
            if occ_start <= end and occ_end >= start:
                item = dict(base)
                item["start_date"] = occ_start.isoformat()
                item["end_date"] = occ_end.isoformat()
                item["occurrence_key"] = f'{base["id"]}-{year}'
                result.append(item)

    result.sort(key=lambda e: (e["start_date"], e.get("start_time") or "", e["title"].lower()))
    return result


def get_colors():
    colors = dict(DEFAULT_COLORS)
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'calendar_color_%'").fetchall()
    for row in rows:
        calendar = row["key"].replace("calendar_color_", "", 1)
        value = row["value"]
        if calendar in COLOR_KEYS and HEX_COLOR_RE.fullmatch(value or ""):
            colors[calendar] = value.lower()
    return colors


def save_colors(payload):
    incoming = payload.get("colors", payload)
    if not isinstance(incoming, dict):
        raise ValueError("Neplatné nastavení barev.")

    updates = {}
    for calendar in COLOR_KEYS:
        if calendar not in incoming:
            continue
        value = str(incoming[calendar]).strip()
        if not HEX_COLOR_RE.fullmatch(value):
            raise ValueError(f"Neplatná barva pro {calendar}.")
        updates[calendar] = value.lower()

    if updates:
        with db() as conn:
            for calendar, value in updates.items():
                conn.execute(
                    '''INSERT INTO settings(key, value, updated_at)
                       VALUES(?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP''',
                    (f"calendar_color_{calendar}", value),
                )
            conn.commit()
    return get_colors()


def read_page(path):
    with open(path, "rb") as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "HanevaHome/0.5.1"

    def send_common_headers(self, status=200, content_type="text/html; charset=utf-8", length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_bytes(self, body, status=200, content_type="text/html; charset=utf-8"):
        self.send_common_headers(status, content_type, len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path, content_type, filename=None):
        try:
            body = Path(path).read_bytes()
        except OSError:
            self.send_json({"error": "Soubor vstupenky nebyl nalezen."}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        if filename:
            fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "ticket"
            from urllib.parse import quote
            self.send_header("Content-Disposition", f"inline; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, status, "application/json; charset=utf-8")

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_page(self, page_path, missing_message):
        try:
            self.send_bytes(read_page(page_path))
        except OSError:
            self.send_bytes((missing_message + "\n").encode("utf-8"), 500, "text/plain; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self.send_bytes(b"ok\n", 200, "text/plain; charset=utf-8")
            return
        if path in ("/", "/index.html"):
            self._send_page(HOME_HTML_PATH, "Home page not found")
            return
        if path in ("/kalendar", "/kalendar/"):
            self._send_page(CALENDAR_HTML_PATH, "Calendar page not found")
            return
        if path in ("/nakupy", "/nakupy/"):
            self._send_page(SHOPPING_HTML_PATH, "Shopping page not found")
            return

        if path == "/api/settings/colors":
            self.send_json({"colors": get_colors(), "defaults": DEFAULT_COLORS})
            return
        if path == "/api/events":
            query = parse_qs(parsed.query)
            today = date.today()
            start_s = query.get("start", [today.replace(day=1).isoformat()])[0]
            end_s = query.get("end", [(today + timedelta(days=45)).isoformat()])[0]
            try:
                self.send_json({"events": events_for_range(start_s, end_s)})
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        ticket_match = re.fullmatch(r"/api/events/(\d+)/tickets/(hanych|eva|[1-9]\d?)(?:/(file|qr))?", path)
        if ticket_match:
            event_id = int(ticket_match.group(1))
            owner = ticket_match.group(2)
            kind = ticket_match.group(3)
            row = get_ticket(event_id, owner)
            if not row:
                self.send_json({"error": "Vstupenka nebyla nalezena."}, 404)
                return
            if not kind:
                self.send_json({"ticket": ticket_metadata(row)})
                return
            name = row["stored_name"] if kind == "file" else row["qr_name"]
            path_on_disk = safe_ticket_path(name)
            if not path_on_disk or (kind == "qr" and not name):
                self.send_json({"error": "QR kód se ve vstupence nepodařilo najít."}, 404)
                return
            download_name = row["original_name"] if kind == "file" else None
            self.send_file(path_on_disk, row["mime_type"] if kind == "file" else "image/png", download_name)
            return
        if path.startswith("/api/events/"):
            try:
                event_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self.send_json({"error": "Neplatné ID."}, 400)
                return
            with db() as conn:
                row = conn.execute(f"SELECT {ticket_select_sql()} FROM events e WHERE e.id=?", (event_id,)).fetchone()
            if not row:
                self.send_json({"error": "Událost nebyla nalezena."}, 404)
                return
            event = row_to_event(row)
            event["tickets"] = [ticket_metadata(ticket) for ticket in get_tickets(event_id)]
            self.send_json({"event": event})
            return

        if path == "/api/shopping/state":
            self.send_json(shopping.get_state())
            return
        if path == "/api/shopping/deals":
            query = parse_qs(parsed.query)
            force = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
            self.send_json(shopping.get_deals(force=force))
            return

        self.send_bytes(b"Not found\n", 404, "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            ticket_match = re.fullmatch(r"/api/events/(\d+)/tickets", path)
            if ticket_match:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0:
                    raise ValueError("Vyber soubor se vstupenkou.")
                if length > MAX_TICKET_SIZE:
                    raise ValueError("Vstupenka může mít nejvýše 15 MB.")
                filename = unquote(self.headers.get("X-Filename", "vstupenka"))
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Soubor nebyl nahrán celý. Zkus to znovu.")
                tickets = save_ticket_bundle(int(ticket_match.group(1)), filename, body)
                self.send_json({"tickets": tickets}, 201)
                return
            if path == "/api/events":
                event = normalize_event(self.read_json())
                with db() as conn:
                    cur = conn.execute(
                        '''INSERT INTO events
                        (title, calendar, start_date, end_date, start_time, end_time, all_day, location, notes, recurrence, event_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (event["title"], event["calendar"], event["start_date"], event["end_date"], event["start_time"],
                         event["end_time"], event["all_day"], event["location"], event["notes"], event["recurrence"], event["event_type"]),
                    )
                    event_id = cur.lastrowid
                    conn.commit()
                    row = conn.execute(f"SELECT {ticket_select_sql()} FROM events e WHERE e.id=?", (event_id,)).fetchone()
                self.send_json({"event": row_to_event(row)}, 201)
                return
            if path == "/api/shopping/items":
                self.send_json({"item": shopping.add_item(self.read_json())}, 201)
                return
            if path == "/api/shopping/watch":
                self.send_json(shopping.set_watched(self.read_json()))
                return
            if path == "/api/shopping/clear-completed":
                self.send_json({"deleted": shopping.clear_completed()})
                return
            self.send_json({"error": "Not found"}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except KeyError as exc:
            self.send_json({"error": str(exc.args[0])}, 404)
        except OSError:
            self.send_json({"error": "Soubor se nepodařilo uložit. Zkontroluj volné místo a zkus to znovu."}, 500)

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/settings/colors":
                colors = save_colors(self.read_json())
                self.send_json({"colors": colors, "defaults": DEFAULT_COLORS})
                return
            if path.startswith("/api/shopping/items/"):
                try:
                    item_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    self.send_json({"error": "Neplatné ID."}, 400)
                    return
                try:
                    item = shopping.update_item(item_id, self.read_json())
                except KeyError as exc:
                    self.send_json({"error": str(exc.args[0])}, 404)
                    return
                self.send_json({"item": item})
                return
            if not path.startswith("/api/events/"):
                self.send_json({"error": "Not found"}, 404)
                return
            try:
                event_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self.send_json({"error": "Neplatné ID."}, 400)
                return
            with db() as conn:
                existing = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not existing:
                self.send_json({"error": "Událost nebyla nalezena."}, 404)
                return
            event = normalize_event(self.read_json(), existing)
            with db() as conn:
                conn.execute(
                    '''UPDATE events SET title=?, calendar=?, start_date=?, end_date=?, start_time=?, end_time=?,
                    all_day=?, location=?, notes=?, recurrence=?, event_type=?, updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                    (event["title"], event["calendar"], event["start_date"], event["end_date"], event["start_time"],
                     event["end_time"], event["all_day"], event["location"], event["notes"], event["recurrence"], event["event_type"], event_id),
                )
                conn.commit()
            if event["event_type"] != "zabava":
                delete_ticket(event_id)
            with db() as conn:
                row = conn.execute(f"SELECT {ticket_select_sql()} FROM events e WHERE e.id=?", (event_id,)).fetchone()
            self.send_json({"event": row_to_event(row)})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self):
        path = urlparse(self.path).path
        bundle_match = re.fullmatch(r"/api/events/(\d+)/tickets", path)
        if bundle_match:
            delete_ticket(int(bundle_match.group(1)))
            self.send_json({"ok": True})
            return
        ticket_match = re.fullmatch(r"/api/events/(\d+)/tickets/(hanych|eva)", path)
        if ticket_match:
            if not delete_ticket(int(ticket_match.group(1)), ticket_match.group(2)):
                self.send_json({"error": "Vstupenka nebyla nalezena."}, 404)
                return
            self.send_json({"ok": True})
            return
        if path.startswith("/api/shopping/items/"):
            try:
                item_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self.send_json({"error": "Neplatné ID."}, 400)
                return
            try:
                shopping.delete_item(item_id)
            except KeyError as exc:
                self.send_json({"error": str(exc.args[0])}, 404)
                return
            self.send_json({"ok": True})
            return
        if not path.startswith("/api/events/"):
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            event_id = int(path.rsplit("/", 1)[1])
        except ValueError:
            self.send_json({"error": "Neplatné ID."}, 400)
            return
        tickets = get_tickets(event_id)
        with db() as conn:
            conn.execute("DELETE FROM event_tickets WHERE event_id=?", (event_id,))
            cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
            conn.commit()
        if cur.rowcount == 0:
            self.send_json({"error": "Událost nebyla nalezena."}, 404)
            return
        for ticket in tickets:
            remove_ticket_files(ticket)
        self.send_json({"ok": True})

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/kalendar", "/kalendar/", "/nakupy", "/nakupy/", "/health"):
            self.send_common_headers(200, "text/html; charset=utf-8", 0)
        else:
            self.send_common_headers(404, "text/plain; charset=utf-8", 0)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Haneva Home listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
