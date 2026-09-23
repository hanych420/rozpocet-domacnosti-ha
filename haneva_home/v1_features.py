from datetime import date, datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
import calendar
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
from collections import Counter
from statistics import median

from PIL import Image, ImageEnhance, ImageFilter
import uuid

import shopping

DB_PATH = "/data/haneva_v1.db"
RECEIPT_DIR = "/data/haneva_receipts"
MAX_UPLOAD = 18 * 1024 * 1024
PERSONS = {"hanych", "eva"}
RESET_DAY_DEFAULT = 6  # Sunday, Python weekday()
SHIFT_VALUES = {"dopoledni", "odpoledni"}

DATE_RE = re.compile(r"\b([0-3]?\d)[.\-/]([01]?\d)(?:[.\-/](20\d{2}|\d{2}))?\b")
MONEY_RE = re.compile(r"(?<!\d)(\d{1,6}(?:[ .]\d{3})*(?:[,.]\d{1,2})?)\s*(?:Kc|Kč|CZK)?\b", re.I)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    Path(RECEIPT_DIR).mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS recipes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          servings INTEGER NOT NULL DEFAULT 2,
          image_url TEXT NOT NULL DEFAULT '',
          ingredients_json TEXT NOT NULL DEFAULT '[]',
          instructions TEXT NOT NULL DEFAULT '',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS recipe_votes(
          recipe_id INTEGER NOT NULL,
          person TEXT NOT NULL,
          cycle_key TEXT NOT NULL,
          vote INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(recipe_id, person, cycle_key)
        );
        CREATE TABLE IF NOT EXISTS planned_recipes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          recipe_id INTEGER NOT NULL,
          cycle_key TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'match',
          cooked INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(recipe_id, cycle_key)
        );
        CREATE TABLE IF NOT EXISTS wishlist(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          url TEXT NOT NULL DEFAULT '',
          price REAL,
          priority INTEGER NOT NULL DEFAULT 2,
          status TEXT NOT NULL DEFAULT 'wanted',
          notes TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS receipts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          shop TEXT NOT NULL DEFAULT '',
          purchased_at TEXT NOT NULL,
          total REAL,
          original_name TEXT NOT NULL DEFAULT '',
          stored_name TEXT NOT NULL DEFAULT '',
          ocr_text TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS receipt_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          receipt_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          name_norm TEXT NOT NULL,
          quantity TEXT NOT NULL DEFAULT '',
          unit_price REAL,
          total_price REAL,
          shopping_item_id INTEGER,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS price_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name_norm TEXT NOT NULL,
          display_name TEXT NOT NULL,
          price REAL NOT NULL,
          quantity_text TEXT NOT NULL DEFAULT '',
          shop TEXT NOT NULL DEFAULT '',
          purchased_at TEXT NOT NULL,
          receipt_id INTEGER,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_price_history_name ON price_history(name_norm,purchased_at);
        CREATE TABLE IF NOT EXISTS work_shifts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          work_date TEXT NOT NULL,
          shift TEXT NOT NULL,
          source_name TEXT NOT NULL DEFAULT '',
          scan_id INTEGER,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(work_date, shift)
        );
        CREATE TABLE IF NOT EXISTS work_scan_logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_name TEXT NOT NULL DEFAULT '',
          image_width INTEGER,
          image_height INTEGER,
          month INTEGER,
          year INTEGER,
          region_left REAL,
          region_right REAL,
          detected_count INTEGER NOT NULL DEFAULT 0,
          diagnostics_json TEXT NOT NULL DEFAULT '[]',
          ocr_text TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        work_columns = {row["name"] for row in conn.execute("PRAGMA table_info(work_shifts)").fetchall()}
        if "scan_id" not in work_columns:
            conn.execute("ALTER TABLE work_shifts ADD COLUMN scan_id INTEGER")
        # Od 1.0.3 je pracovní kalendář záměrně pouze pro odpolední směny.
        conn.execute("DELETE FROM work_shifts WHERE shift='dopoledni'")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('recipe_reset_day',?)", (str(RESET_DAY_DEFAULT),))
        conn.commit()
    calendar_path = "/data/calendar.db"
    if Path(calendar_path).exists():
        try:
            with sqlite3.connect(calendar_path, timeout=10) as calendar_conn:
                calendar_conn.execute(
                    "DELETE FROM events WHERE title='Práce – dopolední' AND notes='Automaticky importováno ze směn Jan Vaněk.'"
                )
                calendar_conn.execute(
                    """UPDATE events SET start_time='14:00',end_time='22:00',all_day=0,updated_at=CURRENT_TIMESTAMP
                       WHERE title='Práce – odpolední' AND notes='Automaticky importováno ze směn Jan Vaněk.'"""
                )
                calendar_conn.commit()
        except sqlite3.Error:
            pass


def _clean(value, limit=500):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def norm(value):
    value = unicodedata.normalize("NFKD", _clean(value, 300).lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute("""INSERT INTO settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""", (key, str(value)))
        conn.commit()


def reset_day():
    try:
        value = int(get_setting("recipe_reset_day", str(RESET_DAY_DEFAULT)))
    except ValueError:
        value = RESET_DAY_DEFAULT
    return value if 0 <= value <= 6 else RESET_DAY_DEFAULT


def cycle_bounds(today=None):
    today = today or date.today()
    rday = reset_day()
    delta = (today.weekday() - rday) % 7
    start = today - timedelta(days=delta)
    end = start + timedelta(days=6)
    return start, end


def cycle_key(today=None):
    start, _ = cycle_bounds(today)
    return start.isoformat()


def _ingredients(value):
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    out = []
    if isinstance(raw, list):
        for entry in raw[:80]:
            if isinstance(entry, str):
                name, qty = _clean(entry, 160), ""
            elif isinstance(entry, dict):
                name, qty = _clean(entry.get("name"), 160), _clean(entry.get("quantity"), 80)
            else:
                continue
            if name:
                out.append({"name": name, "quantity": qty})
    return out


def recipe_dict(row):
    item = dict(row)
    item["active"] = bool(item["active"])
    item["ingredients"] = _ingredients(item.pop("ingredients_json", "[]"))
    return item


def list_recipes(include_inactive=False):
    sql = "SELECT * FROM recipes" + ("" if include_inactive else " WHERE active=1") + " ORDER BY title COLLATE NOCASE"
    with db() as conn:
        return [recipe_dict(r) for r in conn.execute(sql).fetchall()]


def save_recipe(payload, recipe_id=None):
    title = _clean(payload.get("title"), 180)
    if not title:
        raise ValueError("Název receptu je povinný.")
    description = _clean(payload.get("description"), 1000)
    instructions = str(payload.get("instructions") or "").strip()[:12000]
    image_url = _clean(payload.get("image_url"), 1000)
    ingredients = _ingredients(payload.get("ingredients"))
    try:
        servings = max(1, min(int(payload.get("servings") or 2), 30))
    except Exception:
        servings = 2
    data = json.dumps(ingredients, ensure_ascii=False)
    with db() as conn:
        if recipe_id:
            cur = conn.execute("""UPDATE recipes SET title=?,description=?,servings=?,image_url=?,ingredients_json=?,
                instructions=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (title, description, servings, image_url, data, instructions, int(recipe_id)))
            if not cur.rowcount:
                raise KeyError("Recept nebyl nalezen.")
            rid = int(recipe_id)
        else:
            cur = conn.execute("""INSERT INTO recipes(title,description,servings,image_url,ingredients_json,instructions)
                VALUES(?,?,?,?,?,?)""", (title, description, servings, image_url, data, instructions))
            rid = cur.lastrowid
        conn.commit()
        return recipe_dict(conn.execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone())


def delete_recipe(recipe_id):
    with db() as conn:
        cur = conn.execute("UPDATE recipes SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(recipe_id),))
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Recept nebyl nalezen.")


def _ensure_plan_if_match(conn, recipe_id, ckey):
    votes = conn.execute("SELECT person,vote FROM recipe_votes WHERE recipe_id=? AND cycle_key=?", (recipe_id, ckey)).fetchall()
    yes = {r["person"] for r in votes if int(r["vote"]) == 1}
    if PERSONS.issubset(yes):
        conn.execute("INSERT OR IGNORE INTO planned_recipes(recipe_id,cycle_key,source) VALUES(?,?,'match')", (recipe_id, ckey))
        return True
    return False


def vote_recipe(recipe_id, person, vote):
    person = _clean(person, 20).lower()
    if person not in PERSONS:
        raise ValueError("Pro hlasování musí být účet nastavený jako Hanych nebo Eva.")
    ckey = cycle_key()
    value = 1 if bool(vote) else -1
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM recipes WHERE id=? AND active=1", (int(recipe_id),)).fetchone()
        if not exists:
            raise KeyError("Recept nebyl nalezen.")
        conn.execute("""INSERT INTO recipe_votes(recipe_id,person,cycle_key,vote) VALUES(?,?,?,?)
          ON CONFLICT(recipe_id,person,cycle_key) DO UPDATE SET vote=excluded.vote,updated_at=CURRENT_TIMESTAMP""",
          (int(recipe_id), person, ckey, value))
        matched = _ensure_plan_if_match(conn, int(recipe_id), ckey)
        conn.commit()
    return {"matched": matched, "cycle_key": ckey}


def planned_recipes(today=None):
    ckey = cycle_key(today)
    start, end = cycle_bounds(today)
    with db() as conn:
        rows = conn.execute("""SELECT p.id AS plan_id,p.cooked,p.source,r.*
          FROM planned_recipes p JOIN recipes r ON r.id=p.recipe_id
          WHERE p.cycle_key=? ORDER BY p.created_at""", (ckey,)).fetchall()
    result = []
    for row in rows:
        item = recipe_dict(row)
        item["plan_id"] = row["plan_id"]
        item["cooked"] = bool(row["cooked"])
        item["source"] = row["source"]
        result.append(item)
    return {"cycle_key": ckey, "start": start.isoformat(), "end": end.isoformat(), "reset_day": reset_day(), "recipes": result}


def tinder_state(person):
    person = _clean(person, 20).lower()
    ckey = cycle_key()
    with db() as conn:
        rows = conn.execute("""SELECT r.*,v.vote FROM recipes r
          LEFT JOIN recipe_votes v ON v.recipe_id=r.id AND v.person=? AND v.cycle_key=?
          WHERE r.active=1 ORDER BY CASE WHEN v.vote IS NULL THEN 0 ELSE 1 END,r.title COLLATE NOCASE""", (person, ckey)).fetchall()
    return {"cycle_key": ckey, "recipes": [{**recipe_dict(r), "vote": r["vote"]} for r in rows]}


def add_plan(recipe_id):
    ckey = cycle_key()
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM recipes WHERE id=? AND active=1", (int(recipe_id),)).fetchone()
        if not exists:
            raise KeyError("Recept nebyl nalezen.")
        conn.execute("INSERT OR IGNORE INTO planned_recipes(recipe_id,cycle_key,source) VALUES(?,?,'manual')", (int(recipe_id), ckey))
        conn.commit()
    return planned_recipes()


def set_plan_cooked(plan_id, cooked=True):
    with db() as conn:
        cur = conn.execute("UPDATE planned_recipes SET cooked=? WHERE id=?", (1 if cooked else 0, int(plan_id)))
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Plán nebyl nalezen.")


def shopping_preview():
    plan = planned_recipes()
    aggregate = {}
    for recipe in plan["recipes"]:
        for ing in recipe.get("ingredients", []):
            key = norm(ing["name"])
            if not key:
                continue
            row = aggregate.setdefault(key, {"name": ing["name"], "quantities": []})
            if ing.get("quantity"):
                row["quantities"].append(ing["quantity"])
    active = {norm(i["name"]): i for i in shopping.get_state().get("items", []) if not i.get("checked")}
    rows = []
    for key, item in aggregate.items():
        existing = active.get(key)
        rows.append({
            "key": key,
            "name": item["name"],
            "quantity": " + ".join(item["quantities"]),
            "already_on_list": bool(existing),
            "existing_item_id": existing.get("id") if existing else None,
            "selected": not bool(existing),
        })
    return {"items": rows, "cycle_key": plan["cycle_key"]}


def add_preview_to_shopping(payload):
    items = payload.get("items") or []
    added = []
    for row in items:
        if not isinstance(row, dict) or not row.get("selected", True):
            continue
        name = _clean(row.get("name"), 160)
        if not name:
            continue
        item = shopping.add_item({"name": name, "quantity": _clean(row.get("quantity"), 80), "preferred_store": "any", "_source": "recipe"})
        added.append(item)
    return {"added": added, "count": len(added)}


def wishlist_list():
    with db() as conn:
        rows = conn.execute("SELECT * FROM wishlist ORDER BY CASE status WHEN 'wanted' THEN 0 WHEN 'bought' THEN 1 ELSE 2 END,priority ASC,created_at DESC").fetchall()
    return [dict(r) for r in rows]


def save_wishlist(payload, item_id=None):
    title = _clean(payload.get("title"), 200)
    if not title:
        raise ValueError("Název přání je povinný.")
    url = _clean(payload.get("url"), 1500)
    notes = _clean(payload.get("notes"), 2000)
    created_by = _clean(payload.get("created_by"), 40)
    status = _clean(payload.get("status") or "wanted", 20)
    if status not in {"wanted", "bought", "paused"}:
        status = "wanted"
    try:
        priority = max(1, min(int(payload.get("priority") or 2), 3))
    except Exception:
        priority = 2
    try:
        price = float(str(payload.get("price") or "").replace(" ", "").replace(",", ".")) if str(payload.get("price") or "").strip() else None
    except ValueError:
        price = None
    with db() as conn:
        if item_id:
            cur = conn.execute("""UPDATE wishlist SET title=?,url=?,price=?,priority=?,status=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (title, url, price, priority, status, notes, int(item_id)))
            if not cur.rowcount:
                raise KeyError("Přání nebylo nalezeno.")
            iid = int(item_id)
        else:
            cur = conn.execute("INSERT INTO wishlist(title,url,price,priority,status,notes,created_by) VALUES(?,?,?,?,?,?,?)",
                (title, url, price, priority, status, notes, created_by))
            iid = cur.lastrowid
        conn.commit()
        return dict(conn.execute("SELECT * FROM wishlist WHERE id=?", (iid,)).fetchone())


def delete_wishlist(item_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM wishlist WHERE id=?", (int(item_id),))
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Přání nebylo nalezeno.")


def _safe_upload_name(original, suffix):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original or "upload").stem)[:60] or "upload"
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:10]}-{stem}{suffix}"


def detect_upload_type(body):
    if body.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("Nahraj PDF nebo obrázek JPG, PNG či WebP.")


def ocr_file(path, mime_type):
    with tempfile.TemporaryDirectory(prefix="haneva-ocr-") as temp:
        images = []
        if mime_type == "application/pdf":
            prefix = str(Path(temp) / "page")
            subprocess.run(["pdftoppm", "-png", "-r", "180", "-f", "1", "-l", "6", str(path), prefix],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            images = sorted(Path(temp).glob("page-*.png"))
        else:
            images = [Path(path)]
        chunks = []
        for image in images:
            proc = subprocess.run(["tesseract", str(image), "stdout", "-l", "ces", "--psm", "6"],
                                  capture_output=True, timeout=45)
            if proc.returncode == 0:
                chunks.append(proc.stdout.decode("utf-8", "replace"))
        return "\n".join(chunks).strip()


def _parse_date(text):
    for match in DATE_RE.finditer(text):
        d, m, y = int(match.group(1)), int(match.group(2)), match.group(3)
        if not y:
            year = date.today().year
        else:
            year = int(y)
            if year < 100:
                year += 2000
        try:
            return date(year, m, d).isoformat()
        except ValueError:
            pass
    return date.today().isoformat()


def _parse_money(value):
    raw = str(value or "").strip().replace("\u00a0", "").replace(" ", "")
    if not raw:
        return None
    # Czech receipts normally use comma as a decimal separator. Preserve a
    # decimal dot when no comma is present instead of treating it as thousands.
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_receipt_text(text):
    lines = [_clean(line, 300) for line in str(text or "").splitlines() if _clean(line)]
    shop = lines[0][:100] if lines else ""
    purchased_at = _parse_date(text)
    total = None
    for line in reversed(lines):
        low = norm(line)
        if any(k in low for k in ("celkem", "total", "k uhrade", "k platbe", "castka")):
            nums = MONEY_RE.findall(line)
            if nums:
                total = _parse_money(nums[-1])
                if total is not None:
                    break
    items = []
    ignore = ("celkem", "total", "dph", "sazba", "hotovost", "karta", "vraceno", "uctenka", "dic", "ico", "datum", "cas")
    for line in lines:
        low = norm(line)
        if any(word in low for word in ignore):
            continue
        nums = MONEY_RE.findall(line)
        if not nums:
            continue
        price = _parse_money(nums[-1])
        if price is None or price <= 0 or price > 20000:
            continue
        name = re.sub(r"\s+" + re.escape(nums[-1]) + r"\s*(?:Kc|Kč|CZK)?\s*$", "", line, flags=re.I).strip(" -:")
        name = re.sub(r"^\d+[xX*]\s*", "", name).strip()
        if len(name) < 2 or not re.search(r"[A-Za-zÁ-ž]", name):
            continue
        items.append({"name": name[:160], "quantity": "", "price": price})
        if len(items) >= 120:
            break
    return {"shop": shop, "purchased_at": purchased_at, "total": total, "items": items, "ocr_text": text[:30000]}


def scan_receipt(original_name, body):
    if not body or len(body) > MAX_UPLOAD:
        raise ValueError("Účtenka může mít nejvýše 18 MB.")
    mime, suffix = detect_upload_type(body)
    filename = _safe_upload_name(original_name, suffix)
    path = Path(RECEIPT_DIR) / filename
    path.write_bytes(body)
    try:
        text = ocr_file(path, mime)
        parsed = parse_receipt_text(text)
        parsed["stored_name"] = filename
        parsed["original_name"] = Path(original_name or "uctenka").name[:180]
        return parsed
    except Exception:
        path.unlink(missing_ok=True)
        raise ValueError("Text z účtenky se nepodařilo přečíst. Zkus ostřejší fotku nebo PDF.")


def _similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0
    if a == b:
        return 100
    sa, sb = set(a.split()), set(b.split())
    inter = len(sa & sb)
    score = int(100 * (2 * inter) / max(1, len(sa) + len(sb)))
    if a in b or b in a:
        score = max(score, 82)
    return score


def commit_receipt(payload):
    stored_name = Path(_clean(payload.get("stored_name"), 220)).name
    stored = Path(RECEIPT_DIR) / stored_name
    if not stored_name or not stored.exists():
        raise ValueError("Naskenovaný soubor už není dostupný. Nahraj účtenku znovu.")
    shop = _clean(payload.get("shop"), 120)
    purchased_at = _clean(payload.get("purchased_at"), 10) or date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", purchased_at):
        purchased_at = date.today().isoformat()
    try:
        total = float(payload["total"]) if payload.get("total") not in (None, "") else None
    except Exception:
        total = None
    items = payload.get("items") or []
    active = [i for i in shopping.get_state().get("items", []) if not i.get("checked")]
    matched_ids = set()
    with db() as conn:
        cur = conn.execute("INSERT INTO receipts(shop,purchased_at,total,original_name,stored_name,ocr_text) VALUES(?,?,?,?,?,?)",
            (shop, purchased_at, total, _clean(payload.get("original_name"), 180), stored_name, str(payload.get("ocr_text") or "")[:30000]))
        receipt_id = cur.lastrowid
        saved = []
        for raw in items[:160]:
            if not isinstance(raw, dict):
                continue
            name = _clean(raw.get("name"), 160)
            if not name:
                continue
            try:
                price = float(raw.get("price")) if raw.get("price") not in (None, "") else None
            except Exception:
                price = None
            best, best_score = None, 0
            for candidate in active:
                if candidate["id"] in matched_ids:
                    continue
                score = _similar(name, candidate["name"])
                if score > best_score:
                    best, best_score = candidate, score
            shopping_item_id = None
            if best and best_score >= 78:
                shopping_item_id = best["id"]
                matched_ids.add(best["id"])
                shopping.update_item(best["id"], {"checked": True})
            else:
                shopping.record_purchase(name)
            conn.execute("""INSERT INTO receipt_items(receipt_id,name,name_norm,quantity,unit_price,total_price,shopping_item_id)
                VALUES(?,?,?,?,?,?,?)""", (receipt_id, name, norm(name), _clean(raw.get("quantity"), 80), price, price, shopping_item_id))
            if price is not None and price > 0:
                conn.execute("""INSERT INTO price_history(name_norm,display_name,price,quantity_text,shop,purchased_at,receipt_id)
                    VALUES(?,?,?,?,?,?,?)""", (norm(name), name, price, _clean(raw.get("quantity"), 80), shop, purchased_at, receipt_id))
            saved.append({"name": name, "price": price, "matched": bool(shopping_item_id), "shopping_item_id": shopping_item_id})
        conn.commit()
    return {"id": receipt_id, "matched_count": len(matched_ids), "items": saved, "total": total}


def price_history(limit=80):
    with db() as conn:
        rows = conn.execute("""SELECT name_norm,display_name,COUNT(*) AS purchases,
            MIN(price) AS min_price,MAX(price) AS max_price,
            ROUND(AVG(price),2) AS avg_price,
            (SELECT p2.price FROM price_history p2 WHERE p2.name_norm=p.name_norm ORDER BY p2.purchased_at DESC,p2.id DESC LIMIT 1) AS last_price,
            MAX(purchased_at) AS last_bought
          FROM price_history p GROUP BY name_norm,display_name ORDER BY last_bought DESC LIMIT ?""", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def receipt_list(limit=30):
    with db() as conn:
        rows = conn.execute("SELECT id,shop,purchased_at,total,original_name,created_at FROM receipts ORDER BY purchased_at DESC,id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def parse_work_text(text, source_name=""):
    """Fallback pro textové exporty; od 1.0.3 vrací pouze odpolední směny."""
    lines = [_clean(x, 300) for x in str(text or "").splitlines() if _clean(x)]
    result, seen = [], set()
    current_date = None
    current_shift = None
    diagnostics = []
    for line in lines:
        low = norm(line)
        if DATE_RE.search(line):
            current_date = _parse_date(line)
        if "dopoled" in low or "ranni" in low or "rano" in low:
            current_shift = "dopoledni"
        elif "odpoled" in low:
            current_shift = "odpoledni"
        if "jan vanek" in low and current_date:
            accepted = current_shift == "odpoledni"
            diagnostics.append({
                "token": line[:180],
                "date": current_date,
                "decision": current_shift or "nezname",
                "accepted": accepted,
                "reason": "Textový fallback: poslední rozpoznaná hlavička směny je odpolední." if accepted
                          else "Textový fallback: jméno nebylo pod odpolední hlavičkou.",
            })
            if accepted and current_date not in seen:
                seen.add(current_date)
                result.append({"date": current_date, "shift": "odpoledni", "source_name": source_name})
    return result, diagnostics


def _work_norm_token(value):
    value = unicodedata.normalize("NFKD", str(value or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", value)


def _work_parse_date_token(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in {7, 8}:
        return None
    try:
        year = int(digits[-4:])
        month = int(digits[-6:-4])
        day = int(digits[:-6])
        parsed = date(year, month, day)
    except (ValueError, TypeError):
        return None
    if not 2020 <= parsed.year <= 2100:
        return None
    return parsed


def _work_ocr_words(image_path, crop_box=None, requested_scale=3.0, psm=6):
    """OCR jednoho výřezu; souřadnice vrací zpět v pixelech původního screenshotu."""
    with Image.open(image_path) as source:
        source = source.convert("L")
        width, height = source.size
        if crop_box:
            x1, y1, x2, y2 = crop_box
            x1 = max(0, min(width - 1, int(x1)))
            y1 = max(0, min(height - 1, int(y1)))
            x2 = max(x1 + 1, min(width, int(x2)))
            y2 = max(y1 + 1, min(height, int(y2)))
        else:
            x1, y1, x2, y2 = 0, 0, width, height
        region = source.crop((x1, y1, x2, y2))
        scale = min(float(requested_scale), max(1.0, 6500.0 / max(1, region.width)))
        target = (max(1, int(region.width * scale)), max(1, int(region.height * scale)))
        if target != region.size:
            region = region.resize(target, Image.Resampling.LANCZOS)
        region = ImageEnhance.Contrast(region).enhance(1.8)
        region = region.filter(ImageFilter.SHARPEN)
        with tempfile.NamedTemporaryFile(prefix="haneva-work-ocr-", suffix=".png") as temp_image:
            region.save(temp_image.name, "PNG")
            proc = subprocess.run(
                ["tesseract", temp_image.name, "stdout", "-l", "ces", "--psm", str(psm), "tsv"],
                capture_output=True,
                timeout=55,
            )
    if proc.returncode != 0:
        return []
    words = []
    for raw_line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        text = "\t".join(parts[11:]).strip()
        if not text:
            continue
        try:
            left, top, word_w, word_h = map(int, parts[6:10])
            confidence = float(parts[10])
        except ValueError:
            continue
        words.append({
            "text": text,
            "left": x1 + left / scale,
            "top": y1 + top / scale,
            "width": word_w / scale,
            "height": word_h / scale,
            "ocr_confidence": confidence,
        })
    return words


def _work_row_model(date_words):
    points = []
    month_year = Counter()
    for word in date_words:
        parsed = _work_parse_date_token(word.get("text"))
        if not parsed:
            continue
        y = float(word["top"]) + float(word["height"]) / 2
        points.append((parsed.day, parsed.month, parsed.year, y))
        month_year[(parsed.month, parsed.year)] += 1
    if not month_year:
        return None
    (month, year), count = month_year.most_common(1)[0]
    if count < 5:
        return None
    rows = [(day, y) for day, m, yyear, y in points if m == month and yyear == year]
    slopes = []
    for index, (day_a, y_a) in enumerate(rows):
        for day_b, y_b in rows[index + 1:]:
            if day_a == day_b:
                continue
            slope = (y_b - y_a) / (day_b - day_a)
            if 3.0 < slope < 80.0:
                slopes.append(slope)
    if not slopes:
        return None
    spacing = median(slopes)
    intercept = median([y - spacing * (day - 1) for day, y in rows])
    tolerance = max(2.0, spacing * 0.46)
    inliers = [(day, y) for day, y in rows if abs(y - (intercept + spacing * (day - 1))) <= tolerance]
    if len(inliers) >= 5:
        refined = []
        for index, (day_a, y_a) in enumerate(inliers):
            for day_b, y_b in inliers[index + 1:]:
                if day_a == day_b:
                    continue
                slope = (y_b - y_a) / (day_b - day_a)
                if 3.0 < slope < 80.0:
                    refined.append(slope)
        if refined:
            spacing = median(refined)
            intercept = median([y - spacing * (day - 1) for day, y in inliers])
    return {
        "month": month,
        "year": year,
        "spacing": float(spacing),
        "day1_y": float(intercept),
        "days": calendar.monthrange(year, month)[1],
        "date_points": len(inliers),
    }


def _work_name_candidates(words):
    ordered = sorted(words, key=lambda w: (round((w["top"] + w["height"] / 2) / 3), w["left"]))
    result = []
    for index, word in enumerate(ordered):
        result.append(word)
        if index + 1 >= len(ordered):
            continue
        nxt = ordered[index + 1]
        y1 = word["top"] + word["height"] / 2
        y2 = nxt["top"] + nxt["height"] / 2
        gap = nxt["left"] - (word["left"] + word["width"])
        if abs(y1 - y2) <= max(3.0, word["height"], nxt["height"]) and -2 <= gap <= 20:
            result.append({
                "text": word["text"] + " " + nxt["text"],
                "left": word["left"],
                "top": min(word["top"], nxt["top"]),
                "width": max(word["left"] + word["width"], nxt["left"] + nxt["width"]) - word["left"],
                "height": max(word["top"] + word["height"], nxt["top"] + nxt["height"]) - min(word["top"], nxt["top"]),
                "ocr_confidence": min(word.get("ocr_confidence", 0), nxt.get("ocr_confidence", 0)),
            })
    return result


def _work_vertical_boundary(image_path, y1, y2, lo_ratio, hi_ratio, default_ratio):
    """Najde svislý oddělovač tabulky nejblíž očekávané hranici sekce."""
    with Image.open(image_path) as image:
        gray = image.convert("L")
        width, height = gray.size
        top = max(0, min(height - 1, int(y1)))
        bottom = max(top + 1, min(height, int(y2)))
        lo = max(0, int(width * lo_ratio))
        hi = min(width - 1, int(width * hi_ratio))
        pixels = gray.load()
        step_y = max(1, int((bottom - top) / 260))
        scores = []
        for x in range(lo, hi + 1):
            score = 0
            for y in range(top, bottom, step_y):
                # Google Sheets kreslí některé silné oddělovače tmavě šedě,
                # proto je limit schválně vyšší než u OCR textu.
                if pixels[x, y] < 125:
                    score += 1
            scores.append((x, score))
        if not scores:
            return int(width * default_ratio)
        peak = max(score for _, score in scores)
        strong = [(x, score) for x, score in scores if score >= max(12, peak * 0.72)]
        if not strong:
            return int(width * default_ratio)
        expected = width * default_ratio
        return min(strong, key=lambda item: (abs(item[0] - expected), -item[1]))[0]


def _work_extract_afternoons(words, model, source_name, image_width, region_left, region_right):
    target = "janvanek"
    spacing = model["spacing"]
    found = {}
    diagnostics = []
    for word in _work_name_candidates(words):
        token = _work_norm_token(word.get("text"))
        if len(token) < 5:
            continue
        similarity = difflib.SequenceMatcher(None, token, target).ratio()
        if similarity < 0.48:
            continue
        center_x = float(word["left"]) + float(word["width"]) / 2
        center_y = float(word["top"]) + float(word["height"]) / 2
        day = int(round((center_y - model["day1_y"]) / spacing)) + 1
        expected_y = model["day1_y"] + spacing * (day - 1)
        y_delta = abs(center_y - expected_y)
        inside = region_left <= center_x <= region_right
        valid_day = 1 <= day <= model["days"]
        aligned = valid_day and y_delta <= max(3.0, spacing * 0.52)
        accepted = similarity >= 0.72 and inside and aligned
        reason_parts = []
        if not inside:
            reason_parts.append("mimo blok odpolední směny")
        if similarity < 0.72:
            reason_parts.append("jméno se málo podobá Jan Vaněk")
        if not aligned:
            reason_parts.append("nesedí na řádek data")
        if accepted:
            reason_parts.append("jméno je v bloku odpolední směny a sedí na řádek data")
        diagnostics.append({
            "token": word.get("text", "")[:120],
            "similarity": round(similarity * 100, 1),
            "ocr_confidence": round(float(word.get("ocr_confidence", 0)), 1),
            "x": round(center_x, 1),
            "x_percent": round(center_x / max(1.0, float(image_width)) * 100, 1),
            "row_day": day if valid_day else None,
            "y_delta": round(y_delta, 1) if valid_day else None,
            "decision": "odpoledni" if accepted else "odmitnuto",
            "accepted": accepted,
            "reason": "; ".join(reason_parts),
        })
        if not accepted:
            continue
        work_date = date(model["year"], model["month"], day).isoformat()
        previous = found.get(work_date)
        if previous is None or similarity > previous["score"]:
            found[work_date] = {
                "date": work_date,
                "shift": "odpoledni",
                "source_name": source_name,
                "score": similarity,
                "evidence": {
                    "token": word.get("text", "")[:120],
                    "similarity": round(similarity * 100, 1),
                    "x_percent": round(center_x / max(1.0, float(image_width)) * 100, 1),
                    "reason": "Jméno bylo rozpoznáno uvnitř odpoledního bloku a na řádku tohoto data.",
                },
            }
    shifts = []
    for item in sorted(found.values(), key=lambda row: row["date"]):
        item.pop("score", None)
        shifts.append(item)
    return shifts, diagnostics


def _save_work_scan_log(source_name, width, height, model, region_left, region_right, diagnostics, ocr_text):
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO work_scan_logs
               (source_name,image_width,image_height,month,year,region_left,region_right,detected_count,diagnostics_json,ocr_text)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                source_name,
                int(width or 0),
                int(height or 0),
                model.get("month") if model else None,
                model.get("year") if model else None,
                float(region_left) if region_left is not None else None,
                float(region_right) if region_right is not None else None,
                sum(1 for row in diagnostics if row.get("accepted")),
                json.dumps(diagnostics[:250], ensure_ascii=False),
                str(ocr_text or "")[:30000],
            ),
        )
        conn.commit()
        return cur.lastrowid


def _scan_work_image(image_path, source_name):
    with Image.open(image_path) as image:
        width, height = image.size
    date_words = _work_ocr_words(
        image_path,
        (0, 0, max(120, int(width * 0.12)), height),
        requested_scale=4.2,
        psm=6,
    )
    model = _work_row_model(date_words)
    if not model:
        return [], [], "", {"width": width, "height": height, "model": None, "left": None, "right": None}

    top = max(0, int(model["day1_y"] - model["spacing"] * 2.5))
    bottom = min(height, int(model["day1_y"] + model["spacing"] * (model["days"] + 1)))
    left = _work_vertical_boundary(image_path, top, bottom, 0.54, 0.68, 0.615)
    right = _work_vertical_boundary(image_path, top, bottom, 0.81, 0.93, 0.863)
    if right <= left + width * 0.08:
        left, right = int(width * 0.615), int(width * 0.863)

    # Odpolední blok má v používané tabulce čtyři stejně široké sloupce.
    # OCR po jednotlivých sloupcích výrazně omezuje rušení svislými čarami.
    span = right - left
    words = []
    for column in range(4):
        x1 = left + round(span * column / 4) + 3
        x2 = left + round(span * (column + 1) / 4) - 3
        if x2 <= x1:
            continue
        words.extend(_work_ocr_words(image_path, (x1, top, x2, bottom), requested_scale=6.0, psm=6))
    # Jeden společný průchod zachytí případy, kdy OCR rozdělí jméno netypicky.
    words.extend(_work_ocr_words(
        image_path,
        (min(width - 2, left + 2), top, max(left + 4, right - 2), bottom),
        requested_scale=4.3,
        psm=6,
    ))
    shifts, diagnostics = _work_extract_afternoons(words, model, source_name, width, left, right)
    debug_text = " ".join(word["text"] for word in words)[:30000]
    meta = {"width": width, "height": height, "model": model, "left": left, "right": right}
    return shifts, diagnostics, debug_text, meta


def scan_work(original_name, body):
    if not body or len(body) > MAX_UPLOAD:
        raise ValueError("Screenshot může mít nejvýše 18 MB.")
    mime, suffix = detect_upload_type(body)
    source_name = Path(original_name or "smeny").name[:180]
    with tempfile.TemporaryDirectory(prefix="haneva-work-") as temp:
        source = Path(temp) / ("work" + suffix)
        source.write_bytes(body)
        image_path = source
        if mime == "application/pdf":
            rendered = Path(temp) / "work-page.png"
            try:
                subprocess.run(
                    ["pdftoppm", "-singlefile", "-png", "-r", "180", str(source), str(rendered.with_suffix(""))],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=55,
                )
            except (OSError, subprocess.SubprocessError):
                raise ValueError("PDF se nepodařilo převést na obrázek.")
            image_path = rendered
        try:
            shifts, diagnostics, debug_text, meta = _scan_work_image(image_path, source_name)
        except (OSError, ValueError, subprocess.SubprocessError):
            shifts, diagnostics, debug_text = [], [], ""
            with Image.open(image_path) as image:
                meta = {"width": image.width, "height": image.height, "model": None, "left": None, "right": None}

        if not shifts:
            try:
                text = ocr_file(source, mime)
            except Exception:
                text = ""
            fallback, fallback_diag = parse_work_text(text, source_name)
            if fallback:
                shifts = fallback
                diagnostics.extend(fallback_diag)
                debug_text = text[:30000]

        scan_id = _save_work_scan_log(
            source_name,
            meta.get("width"),
            meta.get("height"),
            meta.get("model"),
            meta.get("left"),
            meta.get("right"),
            diagnostics,
            debug_text,
        )
        for shift in shifts:
            shift["scan_id"] = scan_id
        accepted_diagnostics = [row for row in diagnostics if row.get("accepted")]
        return {
            "scan_id": scan_id,
            "ocr_text": debug_text,
            "shifts": shifts,
            "diagnostics": diagnostics,
            "summary": {
                "month": meta.get("model", {}).get("month") if meta.get("model") else None,
                "year": meta.get("model", {}).get("year") if meta.get("model") else None,
                "afternoon_detected": len(shifts),
                "candidates_accepted": len(accepted_diagnostics),
                "region_left_percent": round(meta["left"] / meta["width"] * 100, 1) if meta.get("left") is not None and meta.get("width") else None,
                "region_right_percent": round(meta["right"] / meta["width"] * 100, 1) if meta.get("right") is not None and meta.get("width") else None,
            },
        }


def commit_work(payload):
    shifts = payload.get("shifts") or []
    scan_id = payload.get("scan_id")
    try:
        scan_id = int(scan_id) if scan_id else None
    except (TypeError, ValueError):
        scan_id = None
    added = 0
    accepted = []
    with db() as conn:
        for row in shifts[:100]:
            if not isinstance(row, dict):
                continue
            day = _clean(row.get("date"), 10)
            # Pracovní kalendář od 1.0.3 ukládá výhradně odpolední směny.
            shift = "odpoledni"
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                continue
            cur = conn.execute(
                """INSERT INTO work_shifts(work_date,shift,source_name,scan_id) VALUES(?,?,?,?)
                   ON CONFLICT(work_date,shift) DO UPDATE SET
                     source_name=excluded.source_name,scan_id=COALESCE(excluded.scan_id,work_shifts.scan_id)""",
                (day, shift, _clean(row.get("source_name"), 180), scan_id or row.get("scan_id")),
            )
            added += max(0, cur.rowcount)
            accepted.append(day)
        conn.commit()

    # Zachováme propojení s hlavním kalendářem, ale pouze jako odpolední 14:00–22:00.
    calendar_path = "/data/calendar.db"
    if accepted and Path(calendar_path).exists():
        with sqlite3.connect(calendar_path, timeout=10) as conn:
            for day in accepted:
                title = "Práce – odpolední"
                marker = "Automaticky importováno ze směn Jan Vaněk."
                existing = conn.execute(
                    "SELECT id FROM events WHERE start_date=? AND title=? AND notes=? LIMIT 1",
                    (day, title, marker),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE events SET calendar='hanych',end_date=?,start_time='14:00',end_time='22:00',
                           all_day=0,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (day, existing[0]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO events
                          (title,calendar,start_date,end_date,start_time,end_time,all_day,location,notes,recurrence,event_type)
                          VALUES(?,?,?,?,?,?,?,?,?,'none','')""",
                        (title, "hanych", day, day, "14:00", "22:00", 0, "", marker),
                    )
            conn.commit()
    return {"added": added, "shifts": work_shifts()}


def work_shifts(start=None, end=None):
    start = start or (date.today() - timedelta(days=40)).isoformat()
    end = end or (date.today() + timedelta(days=120)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM work_shifts
               WHERE work_date BETWEEN ? AND ? AND shift='odpoledni'
               ORDER BY work_date""",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def work_scan_logs(limit=10):
    limit = max(1, min(int(limit or 10), 30))
    with db() as conn:
        rows = conn.execute(
            """SELECT id,source_name,image_width,image_height,month,year,region_left,region_right,
                      detected_count,diagnostics_json,created_at
               FROM work_scan_logs ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["diagnostics"] = json.loads(item.pop("diagnostics_json") or "[]")
        except Exception:
            item["diagnostics"] = []
        if item.get("image_width"):
            item["region_left_percent"] = round((item.get("region_left") or 0) / item["image_width"] * 100, 1)
            item["region_right_percent"] = round((item.get("region_right") or 0) / item["image_width"] * 100, 1)
        result.append(item)
    return result


def _budget_dashboard():
    host = os.environ.get("HANEVA_BUDGET_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("HANEVA_BUDGET_PORT", "8099"))
    except ValueError:
        port = 8099
    conn = HTTPConnection(host, port, timeout=8)
    try:
        conn.request("GET", "/api/dashboard", headers={"Accept": "application/json"})
        response = conn.getresponse()
        body = response.read()
        if response.status >= 400:
            return {}
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}
    finally:
        conn.close()


def _month_number(value):
    s = norm(value)
    months = {"leden":1,"unor":2,"brezen":3,"duben":4,"kveten":5,"cerven":6,"cervenec":7,"srpen":8,"zari":9,"rijen":10,"listopad":11,"prosinec":12}
    for k,v in months.items():
        if k in s:
            return v
    m = re.search(r"\b(20\d{2})[-/.](0?[1-9]|1[0-2])\b", str(value or ""))
    if m:
        return int(m.group(2))
    return None


def _expense_number(d):
    preferred = ("expenses","expense","spend","spent","outgoing","outgoings","vydaje","vydaj","shared_expenses","total_expenses")
    for key, value in d.items():
        k = norm(key).replace(" ", "_")
        if any(p in k for p in preferred) and isinstance(value, (int,float)) and value >= 0:
            return float(value)
    return None


def _collect_months(obj, out=None, depth=0):
    out = out or []
    if depth > 7:
        return out
    if isinstance(obj, dict):
        month = None
        for key in ("month","label","name","period","date"):
            if key in obj:
                month = _month_number(obj[key])
                if month:
                    break
        expense = _expense_number(obj)
        if month and expense is not None:
            out.append({"month": month, "expenses": expense, "raw": obj})
        for value in obj.values():
            _collect_months(value, out, depth+1)
    elif isinstance(obj, list):
        for value in obj:
            _collect_months(value, out, depth+1)
    return out


def finance_insights():
    dashboard = _budget_dashboard()
    today = date.today()
    months = _collect_months(dashboard, [])
    dedup = {}
    for row in months:
        # keep the largest plausible expense number for a month; dashboard often contains summary + chart variants
        m = row["month"]
        if row["expenses"] > dedup.get(m, {}).get("expenses", -1):
            dedup[m] = row
    ordered = [dedup[m] for m in sorted(dedup)]
    current = dedup.get(today.month)
    projection = None
    if current:
        days = calendar.monthrange(today.year, today.month)[1]
        projection = round(current["expenses"] / max(1, today.day) * days, 2)
    previous = [r["expenses"] for m,r in dedup.items() if m < today.month]
    avg = round(sum(previous)/len(previous), 2) if previous else None
    anomaly = None
    if current and avg and avg > 0:
        pct = round((current["expenses"] - avg) / avg * 100)
        if abs(pct) >= 20:
            anomaly = {"percent": pct, "message": f"Aktuální měsíc je zatím o {abs(pct)} % {'výš' if pct>0 else 'níž'} než průměr předchozích měsíců."}
    shared_balance = None
    try:
        shared_balance = dashboard.get("shared",{}).get("joint",{}).get("balance")
    except Exception:
        pass
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_month": today.month,
        "current_expenses": current["expenses"] if current else None,
        "projection": projection,
        "previous_average": avg,
        "anomaly": anomaly,
        "months": [{"month": r["month"], "expenses": r["expenses"]} for r in ordered],
        "shared_balance": shared_balance,
        "weekly": _weekly_household_summary(),
        "year": today.year,
    }


def _weekly_household_summary():
    since = (date.today() - timedelta(days=6)).isoformat()
    with db() as conn:
        receipt = conn.execute("SELECT COUNT(*) AS cnt,COALESCE(SUM(total),0) AS total FROM receipts WHERE purchased_at>=?", (since,)).fetchone()
        products = conn.execute("SELECT COUNT(*) AS cnt FROM receipt_items ri JOIN receipts r ON r.id=ri.receipt_id WHERE r.purchased_at>=?", (since,)).fetchone()
    return {"since": since, "receipts": receipt["cnt"], "receipt_total": round(receipt["total"] or 0,2), "products": products["cnt"]}
