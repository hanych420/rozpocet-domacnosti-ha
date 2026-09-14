from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import date, timedelta
import json
import os
import re
import sqlite3

HOST = "0.0.0.0"
PORT = 8100
DB_PATH = "/data/calendar.db"
HOME_HTML_PATH = "/app/home.html"
CALENDAR_HTML_PATH = "/app/calendar.html"
ALLOWED_CALENDARS = {"hanych", "eva", "spolecne", "narozeniny", "kumi"}
DEFAULT_COLORS = {
    "hanych": "#60a5fa",
    "eva": "#c084fc",
    "spolecne": "#34d399",
    "narozeniny": "#fb923c",
    "kumi": "#6b7280",
}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()


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
    }


def row_to_event(row):
    item = dict(row)
    item["all_day"] = bool(item["all_day"])
    item["source_start_date"] = item["start_date"]
    item["source_end_date"] = item["end_date"]
    return item


def events_for_range(start_s, end_s):
    start = valid_date(start_s)
    end = valid_date(end_s)
    if not start or not end or end < start:
        raise ValueError("Neplatný rozsah.")

    result = []
    with db() as conn:
        normal = conn.execute(
            "SELECT * FROM events WHERE recurrence='none' AND start_date <= ? AND end_date >= ? ORDER BY start_date, start_time",
            (end.isoformat(), start.isoformat()),
        ).fetchall()
        recurring = conn.execute("SELECT * FROM events WHERE recurrence='yearly' ORDER BY start_date, start_time").fetchall()

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
        if calendar in ALLOWED_CALENDARS and HEX_COLOR_RE.fullmatch(value or ""):
            colors[calendar] = value.lower()
    return colors


def save_colors(payload):
    incoming = payload.get("colors", payload)
    if not isinstance(incoming, dict):
        raise ValueError("Neplatné nastavení barev.")

    updates = {}
    for calendar in ALLOWED_CALENDARS:
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
    server_version = "HanevaHome/0.3.2"

    def send_common_headers(self, status=200, content_type="text/html; charset=utf-8", length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_bytes(self, body, status=200, content_type="text/html; charset=utf-8"):
        self.send_common_headers(status, content_type, len(body))
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self.send_bytes(b"ok\n", 200, "text/plain; charset=utf-8")
            return
        if path in ("/", "/index.html"):
            try:
                self.send_bytes(read_page(HOME_HTML_PATH))
            except OSError:
                self.send_bytes(b"Home page not found\n", 500, "text/plain; charset=utf-8")
            return
        if path in ("/kalendar", "/kalendar/"):
            try:
                self.send_bytes(read_page(CALENDAR_HTML_PATH))
            except OSError:
                self.send_bytes(b"Calendar page not found\n", 500, "text/plain; charset=utf-8")
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
        if path.startswith("/api/events/"):
            try:
                event_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self.send_json({"error": "Neplatné ID."}, 400)
                return
            with db() as conn:
                row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not row:
                self.send_json({"error": "Událost nebyla nalezena."}, 404)
                return
            self.send_json({"event": row_to_event(row)})
            return
        self.send_bytes(b"Not found\n", 404, "text/plain; charset=utf-8")

    def do_POST(self):
        if urlparse(self.path).path != "/api/events":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            event = normalize_event(self.read_json())
            with db() as conn:
                cur = conn.execute(
                    '''INSERT INTO events
                    (title, calendar, start_date, end_date, start_time, end_time, all_day, location, notes, recurrence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (event["title"], event["calendar"], event["start_date"], event["end_date"], event["start_time"],
                     event["end_time"], event["all_day"], event["location"], event["notes"], event["recurrence"]),
                )
                event_id = cur.lastrowid
                conn.commit()
                row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            self.send_json({"event": row_to_event(row)}, 201)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_PUT(self):
        path = urlparse(self.path).path

        if path == "/api/settings/colors":
            try:
                colors = save_colors(self.read_json())
                self.send_json({"colors": colors, "defaults": DEFAULT_COLORS})
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, 400)
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
        try:
            event = normalize_event(self.read_json(), existing)
            with db() as conn:
                conn.execute(
                    '''UPDATE events SET title=?, calendar=?, start_date=?, end_date=?, start_time=?, end_time=?,
                    all_day=?, location=?, notes=?, recurrence=?, updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                    (event["title"], event["calendar"], event["start_date"], event["end_date"], event["start_time"],
                     event["end_time"], event["all_day"], event["location"], event["notes"], event["recurrence"], event_id),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            self.send_json({"event": row_to_event(row)})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/events/"):
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            event_id = int(path.rsplit("/", 1)[1])
        except ValueError:
            self.send_json({"error": "Neplatné ID."}, 400)
            return
        with db() as conn:
            cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
            conn.commit()
        if cur.rowcount == 0:
            self.send_json({"error": "Událost nebyla nalezena."}, 404)
            return
        self.send_json({"ok": True})

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/kalendar", "/kalendar/", "/health"):
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
