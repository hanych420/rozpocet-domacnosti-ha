from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import date, timedelta
import json
import os
import sqlite3

HOST = "0.0.0.0"
PORT = 8100
DB_PATH = "/data/calendar.db"
CALENDAR_HTML_PATH = "/app/calendar.html"

HOME_HTML = r'''<!doctype html>
<html lang="cs">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#111827">
  <title>Haneva Home</title>
  <style>
    :root { color-scheme: light; --bg:#f5f7fb; --surface:rgba(255,255,255,.88); --surface-solid:#fff; --text:#172033; --muted:#6b7280; --shadow:0 18px 50px rgba(31,41,55,.10); --radius:26px; }
    * { box-sizing:border-box; } html { -webkit-text-size-adjust:100%; }
    body { margin:0; min-height:100vh; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--text); background:radial-gradient(circle at 10% 0%,rgba(147,197,253,.35),transparent 32rem),radial-gradient(circle at 100% 10%,rgba(216,180,254,.30),transparent 30rem),var(--bg); }
    .wrap { width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:28px 0 48px; }
    header { display:flex; align-items:center; justify-content:space-between; gap:20px; margin-bottom:54px; }
    .brand { display:flex; align-items:center; gap:12px; font-weight:800; letter-spacing:-.02em; font-size:19px; }
    .brand-mark { width:42px; height:42px; display:grid; place-items:center; border-radius:14px; background:#111827; color:#fff; box-shadow:0 10px 24px rgba(17,24,39,.18); }
    .today { color:var(--muted); font-size:14px; font-weight:650; text-align:right; }
    .hero { max-width:760px; margin-bottom:34px; }
    .eyebrow { display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border-radius:999px; background:rgba(255,255,255,.68); border:1px solid rgba(255,255,255,.85); color:#596174; font-size:13px; font-weight:750; box-shadow:0 8px 28px rgba(31,41,55,.05); }
    h1 { margin:18px 0 12px; font-size:clamp(40px,7vw,72px); line-height:.98; letter-spacing:-.055em; max-width:820px; }
    .lead { margin:0; color:var(--muted); font-size:clamp(17px,2.2vw,21px); line-height:1.55; max-width:680px; }
    .grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; }
    .card { position:relative; overflow:hidden; min-height:280px; padding:26px; border-radius:var(--radius); background:var(--surface); border:1px solid rgba(255,255,255,.88); box-shadow:var(--shadow); backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px); display:flex; flex-direction:column; justify-content:space-between; text-decoration:none; color:inherit; transition:transform .18s ease,box-shadow .18s ease; }
    a.card:hover { transform:translateY(-3px); box-shadow:0 24px 60px rgba(31,41,55,.14); }
    .card::after { content:""; position:absolute; width:190px; height:190px; border-radius:50%; right:-55px; bottom:-70px; opacity:.9; pointer-events:none; }
    .budget::after { background:rgba(110,231,183,.32); } .calendar::after { background:rgba(167,139,250,.28); } .shopping::after { background:rgba(251,191,36,.24); }
    .icon { width:58px; height:58px; display:grid; place-items:center; border-radius:18px; background:var(--surface-solid); box-shadow:0 12px 30px rgba(31,41,55,.10); font-size:28px; position:relative; z-index:1; }
    .card-content { position:relative; z-index:1; }
    .card h2 { font-size:30px; line-height:1.08; letter-spacing:-.035em; margin:28px 0 8px; }
    .card p { margin:0; color:var(--muted); line-height:1.5; max-width:410px; }
    .bottom { position:relative; z-index:1; display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:28px; }
    .status { display:inline-flex; align-items:center; gap:8px; color:#556070; font-size:13px; font-weight:750; }
    .dot { width:8px; height:8px; border-radius:50%; background:#34d399; box-shadow:0 0 0 5px rgba(52,211,153,.12); }
    .dot.shopping-waiting { background:#f59e0b; box-shadow:0 0 0 5px rgba(245,158,11,.12); }
    .go { width:42px; height:42px; border-radius:14px; display:grid; place-items:center; background:#111827; color:#fff; font-size:20px; font-weight:800; }
    .disabled { cursor:default; } .disabled .go { background:#eef0f5; color:#9ca3af; font-size:12px; width:auto; padding:0 14px; }
    footer { color:#9aa1ad; text-align:center; font-size:12px; margin-top:30px; }
    @media (max-width:980px) { .grid { grid-template-columns:1fr 1fr; } }
    @media (max-width:720px) { .wrap{width:min(100% - 22px,1120px);padding-top:18px} header{margin-bottom:42px}.today{font-size:12px}.brand{font-size:17px}.brand-mark{width:38px;height:38px;border-radius:12px}.hero{margin-bottom:26px} h1{font-size:clamp(42px,13vw,60px)}.lead{font-size:17px}.grid{grid-template-columns:1fr;gap:14px}.card{min-height:235px;padding:22px;border-radius:23px}.card h2{font-size:28px} }
  </style>
</head>
<body>
  <main class="wrap">
    <header><div class="brand"><div class="brand-mark">H</div><span>Haneva</span></div><div class="today" id="today">Haneva Home</div></header>
    <section class="hero"><div class="eyebrow">🏠 Hanych + Eva</div><h1>Všechno naše<br>na jednom místě.</h1><p class="lead">Společný domov pro rozpočet, kalendář a další malé aplikace, které nám usnadní každodenní život.</p></section>
    <section class="grid" aria-label="Aplikace Haneva">
      <a class="card budget" href="https://rozpocet.haneva.cz" aria-label="Otevřít Rozpočet domácnosti"><div class="card-content"><div class="icon">💰</div><h2>Rozpočet</h2><p>Příjmy, osobní a společné výdaje i přehled toho, co nám v měsíci zbývá.</p></div><div class="bottom"><span class="status"><span class="dot"></span>Aktivní</span><span class="go">→</span></div></a>
      <a class="card calendar" href="/kalendar" aria-label="Otevřít společný kalendář"><div class="card-content"><div class="icon">📅</div><h2>Kalendář</h2><p>Hanych, Eva, společné akce, narozeniny a všechno, na co nechceme zapomenout.</p></div><div class="bottom"><span class="status"><span class="dot"></span>Aktivní</span><span class="go">→</span></div></a>
      <article class="card shopping disabled" aria-label="Nákupy se připravují"><div class="card-content"><div class="icon">🛒</div><h2>Nákupy</h2><p>Společný nákupní seznam na jídlo, drogerii a další věci, které je potřeba doma dokoupit.</p></div><div class="bottom"><span class="status"><span class="dot shopping-waiting"></span>Připravujeme</span><span class="go">BRZY</span></div></article>
    </section>
    <footer>Haneva Home · běží doma na Raspberry Pi</footer>
  </main>
  <script>const el=document.getElementById('today');const now=new Date();const text=new Intl.DateTimeFormat('cs-CZ',{weekday:'long',day:'numeric',month:'long'}).format(now);el.textContent=text.charAt(0).toUpperCase()+text.slice(1);</script>
</body>
</html>'''


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
    if calendar not in {"hanych", "eva", "spolecne", "narozeniny"}:
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
        "title": title[:200], "calendar": calendar, "start_date": start_date, "end_date": end_date,
        "start_time": start_time[:5], "end_time": end_time[:5], "all_day": 1 if all_day else 0,
        "location": location[:250], "notes": notes[:2000], "recurrence": recurrence,
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

    years = range(start.year - 1, end.year + 2)
    for row in recurring:
        base = row_to_event(row)
        base_start = date.fromisoformat(base["start_date"])
        base_end = date.fromisoformat(base["end_date"])
        duration = base_end - base_start
        for year in years:
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


class Handler(BaseHTTPRequestHandler):
    server_version = "HanevaHome/0.2"

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
            self.send_bytes(HOME_HTML.encode("utf-8"))
            return
        if path in ("/kalendar", "/kalendar/"):
            try:
                with open(CALENDAR_HTML_PATH, "rb") as f:
                    self.send_bytes(f.read())
            except OSError:
                self.send_bytes(b"Calendar page not found\n", 500, "text/plain; charset=utf-8")
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
