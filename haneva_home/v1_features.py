from datetime import date, datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
import calendar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
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
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(work_date, shift)
        );
        """)
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('recipe_reset_day',?)", (str(RESET_DAY_DEFAULT),))
        conn.commit()


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
    lines = [_clean(x, 300) for x in str(text or "").splitlines() if _clean(x)]
    result, seen = [], set()
    current_date = None
    current_shift = None
    for line in lines:
        low = norm(line)
        dm = DATE_RE.search(line)
        if dm:
            current_date = _parse_date(line)
        if "dopoled" in low or "ranni" in low or "rano" in low:
            current_shift = "dopoledni"
        elif "odpoled" in low:
            current_shift = "odpoledni"
        if "jan vanek" in low and current_date and current_shift:
            key = (current_date, current_shift)
            if key not in seen:
                seen.add(key)
                result.append({"date": current_date, "shift": current_shift, "source_name": source_name})
    return result


def scan_work(original_name, body):
    if not body or len(body) > MAX_UPLOAD:
        raise ValueError("Screenshot může mít nejvýše 18 MB.")
    mime, suffix = detect_upload_type(body)
    with tempfile.TemporaryDirectory(prefix="haneva-work-") as temp:
        path = Path(temp) / ("work" + suffix)
        path.write_bytes(body)
        try:
            text = ocr_file(path, mime)
        except Exception:
            raise ValueError("Screenshot se nepodařilo přečíst.")
    return {"ocr_text": text[:30000], "shifts": parse_work_text(text, Path(original_name or "smeny").name[:180])}


def commit_work(payload):
    shifts = payload.get("shifts") or []
    added = 0
    accepted = []
    with db() as conn:
        for row in shifts[:100]:
            if not isinstance(row, dict):
                continue
            day = _clean(row.get("date"), 10)
            shift = _clean(row.get("shift"), 20)
            if shift not in SHIFT_VALUES or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                continue
            cur = conn.execute("INSERT OR IGNORE INTO work_shifts(work_date,shift,source_name) VALUES(?,?,?)",
                               (day, shift, _clean(row.get("source_name"), 180)))
            added += cur.rowcount
            accepted.append((day, shift))
        conn.commit()
    # Work shifts are also real calendar events. Keep the import idempotent.
    calendar_path = "/data/calendar.db"
    if accepted and Path(calendar_path).exists():
        with sqlite3.connect(calendar_path, timeout=10) as conn:
            for day, shift in accepted:
                title = "Práce – dopolední" if shift == "dopoledni" else "Práce – odpolední"
                marker = "Automaticky importováno ze směn Jan Vaněk."
                exists = conn.execute(
                    "SELECT 1 FROM events WHERE calendar='hanych' AND start_date=? AND title=? AND notes=? LIMIT 1",
                    (day, title, marker),
                ).fetchone()
                if not exists:
                    conn.execute("""INSERT INTO events
                      (title,calendar,start_date,end_date,start_time,end_time,all_day,location,notes,recurrence,event_type)
                      VALUES(?,?,?,?,?,?,?,?,?,'none','')""",
                      (title, "hanych", day, day, "", "", 1, "", marker))
            conn.commit()
    return {"added": added, "shifts": work_shifts()}


def work_shifts(start=None, end=None):
    start = start or (date.today() - timedelta(days=40)).isoformat()
    end = end or (date.today() + timedelta(days=120)).isoformat()
    with db() as conn:
        rows = conn.execute("SELECT * FROM work_shifts WHERE work_date BETWEEN ? AND ? ORDER BY work_date,shift", (start, end)).fetchall()
    return [dict(r) for r in rows]


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
