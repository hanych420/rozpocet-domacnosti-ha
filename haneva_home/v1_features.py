from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from difflib import SequenceMatcher
from statistics import median, mean
import base64
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
import uuid

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import shopping

DB_PATH = "/data/haneva_v1.db"
CALENDAR_DB_PATH = "/data/calendar.db"
RECEIPT_DIR = "/data/haneva_receipts"
RECIPE_IMAGE_DIR = "/data/haneva_recipe_images"
PLATE_PATH = "/data/haneva_plate_reference.png"
OPTIONS_PATH = "/data/options.json"
FOOD_HTML_PATH = "/app/food.html"
WISHLIST_HTML_PATH = "/app/wishlist.html"
INSIGHTS_HTML_PATH = "/app/insights.html"
MAX_UPLOAD = 18 * 1024 * 1024
RESET_WEEKDAY_DEFAULT = 6  # Sunday, Python weekday()
ALLOWED_PEOPLE = {"hanych", "eva"}
MONEY_RE = re.compile(r"(?<!\d)(\d{1,6}(?:[ .]\d{3})*(?:[,.]\d{1,2})?)(?:\s*(?:Kč|CZK))?(?!\d)", re.I)
DATE_RE = re.compile(r"\b([0-3]?\d)[./-]([01]?\d)(?:[./-](20\d{2}|\d{2}))?\b")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs("/data", exist_ok=True)
    Path(RECEIPT_DIR).mkdir(parents=True, exist_ok=True)
    Path(RECIPE_IMAGE_DIR).mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            instructions TEXT NOT NULL DEFAULT '',
            servings INTEGER NOT NULL DEFAULT 2,
            image_name TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            name_norm TEXT NOT NULL,
            amount REAL,
            unit TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id, position);
        CREATE TABLE IF NOT EXISTS recipe_votes (
            recipe_id INTEGER NOT NULL,
            cycle_key TEXT NOT NULL,
            person TEXT NOT NULL,
            vote TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(recipe_id, cycle_key, person)
        );
        CREATE TABLE IF NOT EXISTS planned_recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            cycle_key TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'match',
            cooked_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(recipe_id, cycle_key)
        );
        CREATE INDEX IF NOT EXISTS idx_planned_cycle ON planned_recipes(cycle_key, created_at);
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT NOT NULL UNIQUE,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            store TEXT NOT NULL DEFAULT '',
            purchased_at TEXT,
            total REAL,
            raw_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            name_norm TEXT NOT NULL,
            quantity REAL,
            unit_price REAL,
            total_price REAL,
            shopping_item_id INTEGER,
            match_confidence REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt ON receipt_items(receipt_id);
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_item_id INTEGER NOT NULL UNIQUE,
            name_norm TEXT NOT NULL,
            display_name TEXT NOT NULL,
            price REAL NOT NULL,
            store TEXT NOT NULL DEFAULT '',
            purchased_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_price_history_name ON price_history(name_norm, purchased_at);
        CREATE TABLE IF NOT EXISTS wishlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            price REAL,
            priority INTEGER NOT NULL DEFAULT 2,
            person TEXT NOT NULL DEFAULT 'spolecne',
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'wanted',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_wishlist_status ON wishlist(status, priority, created_at);
        CREATE TABLE IF NOT EXISTS work_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256 TEXT NOT NULL UNIQUE,
            original_name TEXT NOT NULL,
            raw_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS work_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_date TEXT NOT NULL,
            shift_type TEXT NOT NULL,
            event_id INTEGER,
            import_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(shift_date, shift_type)
        );
        CREATE TABLE IF NOT EXISTS finance_snapshots (
            snapshot_date TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            balance REAL,
            spent REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('reset_weekday',?)",
            (str(RESET_WEEKDAY_DEFAULT),),
        )
        conn.commit()


def clean(value, limit=500):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def norm(value):
    value = unicodedata.normalize("NFKD", clean(value, 300).lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_settings(payload):
    updates = {}
    if "reset_weekday" in payload:
        day = int(payload["reset_weekday"])
        if day < 0 or day > 6:
            raise ValueError("Neplatný den resetu.")
        updates["reset_weekday"] = str(day)
    with db() as conn:
        for key, value in updates.items():
            conn.execute(
                """INSERT INTO settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (key, value),
            )
        conn.commit()
    return settings_state()


def settings_state():
    return {"reset_weekday": int(get_setting("reset_weekday", str(RESET_WEEKDAY_DEFAULT)))}


def cycle_start(on_date=None):
    today = on_date or date.today()
    reset = int(get_setting("reset_weekday", str(RESET_WEEKDAY_DEFAULT)))
    delta = (today.weekday() - reset) % 7
    return today - timedelta(days=delta)


def cycle_key(on_date=None):
    return cycle_start(on_date).isoformat()


def cycle_label(key=None):
    start = date.fromisoformat(key or cycle_key())
    end = start + timedelta(days=6)
    return f"{start.day}. {start.month}. – {end.day}. {end.month}. {end.year}"


def parse_ingredient_line(line):
    line = clean(line, 240)
    if not line:
        return None
    # Friendly parser for: "2 ks cibule", "500 g kuřecí prsa", or just "sůl".
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*([a-zA-Zá-žÁ-Ž]+)?\s+(.+)$", line)
    amount, unit, name = None, "", line
    if m:
        amount = float(m.group(1).replace(",", "."))
        unit = clean(m.group(2) or "", 30)
        name = clean(m.group(3), 160)
    return {"name": name, "name_norm": norm(name), "amount": amount, "unit": unit, "note": ""}


def normalize_ingredients(payload):
    raw = payload.get("ingredients", [])
    result = []
    if isinstance(raw, str):
        raw = raw.splitlines()
    for pos, item in enumerate(raw):
        if isinstance(item, str):
            parsed = parse_ingredient_line(item)
        elif isinstance(item, dict):
            name = clean(item.get("name"), 160)
            if not name:
                continue
            amount = item.get("amount")
            try:
                amount = float(str(amount).replace(",", ".")) if amount not in (None, "") else None
            except ValueError:
                amount = None
            parsed = {
                "name": name,
                "name_norm": norm(name),
                "amount": amount,
                "unit": clean(item.get("unit"), 30),
                "note": clean(item.get("note"), 120),
            }
        else:
            continue
        if parsed and parsed["name_norm"]:
            parsed["position"] = pos
            result.append(parsed)
    return result


def _recipe_row(conn, recipe_id):
    row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if not row:
        raise KeyError("Recept nebyl nalezen.")
    ingredients = [
        dict(x)
        for x in conn.execute(
            "SELECT id,name,name_norm,amount,unit,note,position FROM recipe_ingredients WHERE recipe_id=? ORDER BY position,id",
            (recipe_id,),
        ).fetchall()
    ]
    item = dict(row)
    item["ingredients"] = ingredients
    item["image_url"] = f"/api/v1/recipes/{recipe_id}/image" if item["image_name"] else ""
    return item


def list_recipes(person=""):
    person = person if person in ALLOWED_PEOPLE else ""
    key = cycle_key()
    with db() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM recipes ORDER BY updated_at DESC,id DESC").fetchall()]
        recipes = [_recipe_row(conn, rid) for rid in ids]
        votes = conn.execute(
            "SELECT recipe_id,person,vote FROM recipe_votes WHERE cycle_key=?", (key,)
        ).fetchall()
        planned = conn.execute(
            "SELECT recipe_id,cooked_at FROM planned_recipes WHERE cycle_key=?", (key,)
        ).fetchall()
    vote_map = {}
    for row in votes:
        vote_map.setdefault(row["recipe_id"], {})[row["person"]] = row["vote"]
    planned_map = {row["recipe_id"]: row["cooked_at"] for row in planned}
    for recipe in recipes:
        recipe["votes"] = vote_map.get(recipe["id"], {})
        recipe["planned"] = recipe["id"] in planned_map
        recipe["cooked_at"] = planned_map.get(recipe["id"])
    queue = [r for r in recipes if person and person not in r["votes"]]
    return {
        "recipes": recipes,
        "swipe_queue": queue,
        "cycle_key": key,
        "cycle_label": cycle_label(key),
        "settings": settings_state(),
    }


def create_recipe(payload):
    title = clean(payload.get("title"), 160)
    if not title:
        raise ValueError("Název receptu je povinný.")
    description = clean(payload.get("description"), 800)
    instructions = str(payload.get("instructions") or "").strip()[:8000]
    try:
        servings = max(1, min(20, int(payload.get("servings") or 2)))
    except (TypeError, ValueError):
        servings = 2
    created_by = clean(payload.get("created_by"), 30)
    ingredients = normalize_ingredients(payload)
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO recipes(title,description,instructions,servings,created_by)
               VALUES(?,?,?,?,?)""",
            (title, description, instructions, servings, created_by),
        )
        rid = cur.lastrowid
        conn.executemany(
            """INSERT INTO recipe_ingredients(recipe_id,name,name_norm,amount,unit,note,position)
               VALUES(?,?,?,?,?,?,?)""",
            [(rid, i["name"], i["name_norm"], i["amount"], i["unit"], i["note"], i["position"]) for i in ingredients],
        )
        conn.commit()
        return _recipe_row(conn, rid)


def update_recipe(recipe_id, payload):
    with db() as conn:
        current = _recipe_row(conn, recipe_id)
        title = clean(payload.get("title", current["title"]), 160)
        if not title:
            raise ValueError("Název receptu je povinný.")
        description = clean(payload.get("description", current["description"]), 800)
        instructions = str(payload.get("instructions", current["instructions"]) or "").strip()[:8000]
        try:
            servings = max(1, min(20, int(payload.get("servings", current["servings"]))))
        except (TypeError, ValueError):
            servings = current["servings"]
        conn.execute(
            """UPDATE recipes SET title=?,description=?,instructions=?,servings=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (title, description, instructions, servings, recipe_id),
        )
        if "ingredients" in payload:
            ingredients = normalize_ingredients(payload)
            conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
            conn.executemany(
                """INSERT INTO recipe_ingredients(recipe_id,name,name_norm,amount,unit,note,position)
                   VALUES(?,?,?,?,?,?,?)""",
                [(recipe_id, i["name"], i["name_norm"], i["amount"], i["unit"], i["note"], i["position"]) for i in ingredients],
            )
        conn.commit()
        return _recipe_row(conn, recipe_id)


def delete_recipe(recipe_id):
    with db() as conn:
        row = conn.execute("SELECT image_name FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if not row:
            raise KeyError("Recept nebyl nalezen.")
        conn.execute("DELETE FROM recipe_votes WHERE recipe_id=?", (recipe_id,))
        conn.execute("DELETE FROM planned_recipes WHERE recipe_id=?", (recipe_id,))
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
        conn.execute("DELETE FROM recipes WHERE id=?", (recipe_id,))
        conn.commit()
    if row["image_name"]:
        (Path(RECIPE_IMAGE_DIR) / row["image_name"]).unlink(missing_ok=True)


def vote_recipe(recipe_id, person, vote):
    if person not in ALLOWED_PEOPLE:
        raise ValueError("Vyber Hanych nebo Eva.")
    if vote not in {"like", "nope"}:
        raise ValueError("Neplatná volba.")
    key = cycle_key()
    with db() as conn:
        if not conn.execute("SELECT 1 FROM recipes WHERE id=?", (recipe_id,)).fetchone():
            raise KeyError("Recept nebyl nalezen.")
        conn.execute(
            """INSERT INTO recipe_votes(recipe_id,cycle_key,person,vote,updated_at)
               VALUES(?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(recipe_id,cycle_key,person) DO UPDATE SET vote=excluded.vote,updated_at=CURRENT_TIMESTAMP""",
            (recipe_id, key, person, vote),
        )
        votes = {
            row["person"]: row["vote"]
            for row in conn.execute(
                "SELECT person,vote FROM recipe_votes WHERE recipe_id=? AND cycle_key=?",
                (recipe_id, key),
            ).fetchall()
        }
        matched = votes.get("hanych") == "like" and votes.get("eva") == "like"
        if matched:
            conn.execute(
                "INSERT OR IGNORE INTO planned_recipes(recipe_id,cycle_key,source) VALUES(?,?,'match')",
                (recipe_id, key),
            )
        conn.commit()
    return {"matched": matched, "votes": votes, "cycle_key": key}


def plan_recipe(recipe_id):
    key = cycle_key()
    with db() as conn:
        if not conn.execute("SELECT 1 FROM recipes WHERE id=?", (recipe_id,)).fetchone():
            raise KeyError("Recept nebyl nalezen.")
        conn.execute(
            "INSERT OR IGNORE INTO planned_recipes(recipe_id,cycle_key,source) VALUES(?,?,'manual')",
            (recipe_id, key),
        )
        conn.commit()
    return planned_state()


def planned_state(key=None):
    key = key or cycle_key()
    with db() as conn:
        rows = conn.execute(
            """SELECT p.id AS planned_id,p.recipe_id,p.cooked_at,p.source,p.created_at,
                      r.title,r.description,r.servings,r.image_name
               FROM planned_recipes p JOIN recipes r ON r.id=p.recipe_id
               WHERE p.cycle_key=? ORDER BY p.cooked_at IS NOT NULL,p.created_at""",
            (key,),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["image_url"] = f"/api/v1/recipes/{item['recipe_id']}/image" if item["image_name"] else ""
            item["ingredients"] = [
                dict(x)
                for x in conn.execute(
                    "SELECT name,name_norm,amount,unit,note FROM recipe_ingredients WHERE recipe_id=? ORDER BY position,id",
                    (item["recipe_id"],),
                ).fetchall()
            ]
            items.append(item)
    return {
        "cycle_key": key,
        "cycle_label": cycle_label(key),
        "planned": items,
        "settings": settings_state(),
    }


def mark_cooked(planned_id, cooked=True):
    with db() as conn:
        cur = conn.execute(
            "UPDATE planned_recipes SET cooked_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat() if cooked else None, planned_id),
        )
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Plánovaný recept nebyl nalezen.")
    return planned_state()


def delete_planned(planned_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM planned_recipes WHERE id=?", (planned_id,))
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Plánovaný recept nebyl nalezen.")


def _sum_amount(existing, amount, unit):
    if amount is None:
        return existing
    if existing["amount"] is None:
        existing["amount"] = amount
        existing["unit"] = unit
    elif norm(existing["unit"]) == norm(unit):
        existing["amount"] += amount
    else:
        existing["note_parts"].append(f"{amount:g} {unit}".strip())
    return existing


def shopping_preview():
    plan = planned_state()
    merged = {}
    for recipe in plan["planned"]:
        if recipe["cooked_at"]:
            continue
        for ing in recipe["ingredients"]:
            key = ing["name_norm"]
            if not key:
                continue
            if key not in merged:
                merged[key] = {
                    "name": ing["name"],
                    "name_norm": key,
                    "amount": None,
                    "unit": "",
                    "note_parts": [],
                    "recipes": [],
                }
            _sum_amount(merged[key], ing["amount"], ing["unit"])
            if recipe["title"] not in merged[key]["recipes"]:
                merged[key]["recipes"].append(recipe["title"])
    state = shopping.get_state()
    active_items = [i for i in state["items"] if not i["checked"]]
    active = {norm(i["name"]): i for i in active_items}
    out = []
    for item in merged.values():
        existing = active.get(item["name_norm"])
        similarity = 1.0 if existing else 0.0
        if not existing:
            best = None
            for candidate in active_items:
                score = _token_similarity(item["name"], candidate["name"])
                if score > similarity:
                    best, similarity = candidate, score
            if best and similarity >= 0.78:
                existing = best
        qty = ""
        if item["amount"] is not None:
            qty = f"{item['amount']:g} {item['unit']}".strip()
        if item["note_parts"]:
            qty = " + ".join(([qty] if qty else []) + item["note_parts"])
        out.append({
            "name": item["name"],
            "name_norm": item["name_norm"],
            "quantity": qty,
            "already_on_list": bool(existing),
            "existing_item": existing,
            "match_confidence": round(similarity, 3) if existing else 0,
            "selected": not bool(existing),
            "recipes": item["recipes"],
        })
    out.sort(key=lambda x: (x["already_on_list"], x["name_norm"]))
    return {"items": out, "cycle_key": plan["cycle_key"], "cycle_label": plan["cycle_label"]}


def _merge_quantity_text(existing, incoming):
    existing, incoming = clean(existing, 80), clean(incoming, 80)
    if not existing:
        return incoming
    if not incoming:
        return existing
    rx = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([^\d]+)?$")
    a, b = rx.match(existing), rx.match(incoming)
    if a and b and norm(a.group(2) or "") == norm(b.group(2) or ""):
        total = float(a.group(1).replace(",", ".")) + float(b.group(1).replace(",", "."))
        return f"{total:g} {clean(a.group(2) or '', 30)}".strip()
    if norm(existing) == norm(incoming):
        return existing
    return f"{existing} + {incoming}"[:80]


def commit_shopping(payload):
    items = payload.get("items") or []
    added, skipped = [], []
    for item in items:
        if not item.get("selected", True):
            skipped.append(clean(item.get("name"), 160))
            continue
        name = clean(item.get("name"), 160)
        if not name:
            continue
        existing = item.get("existing_item") if isinstance(item.get("existing_item"), dict) else None
        if existing and existing.get("id"):
            try:
                updated = shopping.update_item(int(existing["id"]), {
                    "name": existing.get("name") or name,
                    "quantity": _merge_quantity_text(existing.get("quantity"), item.get("quantity")),
                    "preferred_store": existing.get("preferred_store") or "any",
                    "checked": False,
                })
                added.append(updated)
                continue
            except (KeyError, ValueError, TypeError):
                pass
        added.append(shopping.add_item({
            "name": name,
            "quantity": clean(item.get("quantity"), 80),
            "preferred_store": "any",
        }))
    return {"added": added, "skipped": skipped}


def _mime_from_body(body, filename="", content_type=""):
    if body.startswith(b"%PDF"):
        return "application/pdf", ".pdf"
    if body.startswith(b"\x89PNG"):
        return "image/png", ".png"
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if body[:4] in (b"RIFF",):
        return "image/webp", ".webp"
    low = (content_type or "").lower()
    if "pdf" in low:
        return "application/pdf", ".pdf"
    raise ValueError("Nahraj PDF nebo fotku účtenky (JPG/PNG).")


def _ocr_images(path, mime_type, max_pages=6):
    images = []
    temp = tempfile.TemporaryDirectory(prefix="haneva-ocr-")
    root = Path(temp.name)
    if mime_type == "application/pdf":
        prefix = root / "page"
        subprocess.run(
            ["pdftoppm", "-png", "-r", "220", "-f", "1", "-l", str(max_pages), str(path), str(prefix)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        images = sorted(root.glob("page-*.png"))
    else:
        images = [Path(path)]
    return temp, images


def _ocr_text(path, mime_type):
    temp, images = _ocr_images(path, mime_type)
    try:
        texts = []
        for image in images:
            try:
                proc = subprocess.run(
                    ["tesseract", str(image), "stdout", "-l", "ces+eng", "--psm", "6"],
                    check=False, capture_output=True, timeout=40,
                )
            except FileNotFoundError:
                raise ValueError("OCR není v této instalaci dostupné.")
            if proc.returncode == 0:
                texts.append(proc.stdout.decode("utf-8", "replace"))
        text = "\n".join(texts).strip()
        if not text:
            raise ValueError("Z účtenky se nepodařilo přečíst text. Zkus ostřejší fotku.")
        return text
    finally:
        temp.cleanup()


def _money(value):
    value = str(value or "").replace("\u00a0", " ").strip()
    value = re.sub(r"(?<=\d)[ .](?=\d{3}\b)", "", value)
    value = value.replace(",", ".")
    try:
        return float(re.sub(r"[^0-9.-]", "", value))
    except ValueError:
        return None


def _receipt_date(text):
    for m in DATE_RE.finditer(text):
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if not year:
            year = date.today().year
        else:
            year = int(year)
            if year < 100:
                year += 2000
        try:
            value = date(year, month, day)
        except ValueError:
            continue
        if date.today() - timedelta(days=370) <= value <= date.today() + timedelta(days=5):
            return value.isoformat()
    return date.today().isoformat()


def _receipt_store(text):
    candidates = ["Lidl", "Albert", "Kaufland", "Tesco", "Penny", "Billa", "Globus", "Rohlík", "Košík", "Makro"]
    folded = norm(text)
    for store in candidates:
        if norm(store) in folded:
            return store
    lines = [clean(x, 80) for x in text.splitlines() if clean(x, 80)]
    return lines[0] if lines else "Nákup"


def _receipt_total(lines):
    keywords = ("celkem", "k uhrade", "k úhradě", "total", "celkova castka", "celková částka", "platba")
    candidates = []
    for idx, line in enumerate(lines):
        n = norm(line)
        if any(norm(k) in n for k in keywords):
            values = [_money(m.group(1)) for m in MONEY_RE.finditer(line)]
            values = [v for v in values if v is not None]
            if values:
                candidates.append((2 if "celkem" in n or "uhrade" in n else 1, idx, max(values)))
    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[-1][2]
    values = []
    for line in lines[-15:]:
        values.extend(_money(m.group(1)) for m in MONEY_RE.finditer(line))
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _parse_receipt_items(lines):
    blocked = (
        "celkem", "uhrade", "úhradě", "dph", "zaklad", "základ", "hotovost", "karta",
        "visa", "mastercard", "vraceno", "sleva celkem", "subtotal", "total", "dan",
    )
    items = []
    for line in lines:
        raw = clean(line, 240)
        n = norm(raw)
        if len(raw) < 4 or any(norm(x) in n for x in blocked):
            continue
        matches = list(MONEY_RE.finditer(raw))
        if not matches:
            continue
        last = matches[-1]
        price = _money(last.group(1))
        if price is None or price <= 0 or price > 100000:
            continue
        name = clean(raw[:last.start()].strip(" -*:;|"), 160)
        name = re.sub(r"^\d+\s*[xX×]\s*", "", name).strip()
        if len(norm(name)) < 2 or sum(ch.isalpha() for ch in name) < 2:
            continue
        qty = None
        qm = re.search(r"\b(\d+(?:[.,]\d+)?)\s*[xX×]\b", raw)
        if qm:
            try:
                qty = float(qm.group(1).replace(",", "."))
            except ValueError:
                pass
        unit_price = price / qty if qty and qty > 0 else price
        items.append({
            "name": name,
            "name_norm": norm(name),
            "quantity": qty,
            "unit_price": unit_price,
            "total_price": price,
        })
    # Remove obvious OCR duplicates while preserving repeated legitimate lines when price differs.
    seen, out = set(), []
    for item in items:
        key = (item["name_norm"], round(item["total_price"], 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:120]


def _token_similarity(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    seq = SequenceMatcher(None, a, b).ratio()
    containment = 0.94 if (a in b or b in a) and min(len(a), len(b)) >= 4 else 0
    return max(seq, jaccard, containment)


def _increment_completed_history(name):
    n = shopping.normalize_name(name)
    now = datetime.now(timezone.utc).isoformat()
    with shopping.db() as conn:
        conn.execute(
            """INSERT INTO history(name_norm,display_name,times_completed,last_completed_at)
               VALUES(?,?,1,?)
               ON CONFLICT(name_norm) DO UPDATE SET
                 display_name=excluded.display_name,
                 times_completed=history.times_completed+1,
                 last_completed_at=excluded.last_completed_at""",
            (n, clean(name, 160), now),
        )
        conn.commit()


def _match_receipt_items(receipt_id, items, store, purchased_at):
    state = shopping.get_state()
    open_items = [i for i in state["items"] if not i["checked"]]
    checked_items = [i for i in state["items"] if i["checked"]]
    results = []
    for item in items:
        best, score = None, 0.0
        for candidate in open_items:
            s = _token_similarity(item["name"], candidate["name"])
            if s > score:
                best, score = candidate, s
        already_checked = False
        if score < 0.72:
            for candidate in checked_items:
                s = _token_similarity(item["name"], candidate["name"])
                if s > score:
                    best, score, already_checked = candidate, s, True
        shopping_item_id = None
        if best and score >= 0.72:
            shopping_item_id = best["id"]
            if not already_checked and not best["checked"]:
                shopping.update_item(best["id"], {"checked": True})
                best["checked"] = True
        else:
            # Receipt-only purchase counts once as a real completed purchase.
            _increment_completed_history(item["name"])
        with db() as conn:
            cur = conn.execute(
                """INSERT INTO receipt_items(receipt_id,name,name_norm,quantity,unit_price,total_price,shopping_item_id,match_confidence)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (receipt_id, item["name"], item["name_norm"], item["quantity"], item["unit_price"],
                 item["total_price"], shopping_item_id, score if shopping_item_id else 0),
            )
            receipt_item_id = cur.lastrowid
            if item["unit_price"] and item["unit_price"] > 0:
                conn.execute(
                    """INSERT OR IGNORE INTO price_history
                       (receipt_item_id,name_norm,display_name,price,store,purchased_at)
                       VALUES(?,?,?,?,?,?)""",
                    (receipt_item_id, item["name_norm"], item["name"], item["unit_price"], store, purchased_at),
                )
            conn.commit()
        result = dict(item)
        result["shopping_item_id"] = shopping_item_id
        result["match_confidence"] = round(score, 3) if shopping_item_id else 0
        result["matched_name"] = best["name"] if shopping_item_id else ""
        results.append(result)
    return results


def receipt_by_id(receipt_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if not row:
            raise KeyError("Účtenka nebyla nalezena.")
        items = [dict(x) for x in conn.execute(
            "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id", (receipt_id,)
        ).fetchall()]
    data = dict(row)
    data["items"] = items
    data["matched_count"] = sum(1 for x in items if x["shopping_item_id"])
    data["extra_count"] = len(items) - data["matched_count"]
    return data


def process_receipt(body, filename, content_type=""):
    if not body or len(body) > MAX_UPLOAD:
        raise ValueError("Účtenka může mít nejvýše 18 MB.")
    digest = hashlib.sha256(body).hexdigest()
    with db() as conn:
        existing = conn.execute("SELECT id FROM receipts WHERE sha256=?", (digest,)).fetchone()
    if existing:
        result = receipt_by_id(existing["id"])
        result["duplicate"] = True
        return result
    mime, ext = _mime_from_body(body, filename, content_type)
    stored_name = uuid.uuid4().hex + ext
    target = Path(RECEIPT_DIR) / stored_name
    target.write_bytes(body)
    try:
        text = _ocr_text(target, mime)
        lines = [clean(x, 260) for x in text.splitlines() if clean(x, 260)]
        store = _receipt_store(text)
        purchased_at = _receipt_date(text)
        total = _receipt_total(lines)
        items = _parse_receipt_items(lines)
        with db() as conn:
            cur = conn.execute(
                """INSERT INTO receipts(sha256,original_name,stored_name,mime_type,store,purchased_at,total,raw_text)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (digest, clean(filename or "uctenka", 180), stored_name, mime, store, purchased_at, total, text[:50000]),
            )
            receipt_id = cur.lastrowid
            conn.commit()
        matched = _match_receipt_items(receipt_id, items, store, purchased_at)
        result = receipt_by_id(receipt_id)
        result["items"] = matched
        result["duplicate"] = False
        return result
    except Exception:
        target.unlink(missing_ok=True)
        raise


def list_receipts(limit=30):
    with db() as conn:
        rows = conn.execute(
            """SELECT r.*,COUNT(i.id) AS item_count,
                      SUM(CASE WHEN i.shopping_item_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_count
               FROM receipts r LEFT JOIN receipt_items i ON i.receipt_id=r.id
               GROUP BY r.id ORDER BY COALESCE(r.purchased_at,r.created_at) DESC,r.id DESC LIMIT ?""",
            (max(1, min(100, int(limit))),),
        ).fetchall()
    return [dict(x) for x in rows]


def price_history_state(limit=80):
    with db() as conn:
        rows = conn.execute(
            """SELECT name_norm,MAX(display_name) AS display_name,COUNT(*) AS samples,
                      AVG(price) AS average_price,MIN(price) AS min_price,MAX(price) AS max_price,
                      MAX(purchased_at) AS last_purchased
               FROM price_history GROUP BY name_norm
               ORDER BY last_purchased DESC LIMIT ?""",
            (max(1, min(200, int(limit))),),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            last = conn.execute(
                """SELECT price,store,purchased_at FROM price_history
                   WHERE name_norm=? ORDER BY purchased_at DESC,id DESC LIMIT 1""",
                (row["name_norm"],),
            ).fetchone()
            item["last_price"] = last["price"] if last else None
            item["last_store"] = last["store"] if last else ""
            result.append(item)
    return result


def wishlist_state():
    with db() as conn:
        rows = [dict(x) for x in conn.execute(
            """SELECT * FROM wishlist
               ORDER BY CASE status WHEN 'wanted' THEN 0 WHEN 'bought' THEN 1 ELSE 2 END,
                        priority ASC,created_at DESC"""
        ).fetchall()]
    return {"items": rows}


def save_wishlist(payload, item_id=None):
    title = clean(payload.get("title"), 180)
    if not title:
        raise ValueError("Název přání je povinný.")
    url = clean(payload.get("url"), 1000)
    price = payload.get("price")
    try:
        price = float(str(price).replace(",", ".")) if price not in (None, "") else None
    except ValueError:
        raise ValueError("Cena není platné číslo.")
    priority = max(1, min(3, int(payload.get("priority") or 2)))
    person = clean(payload.get("person") or "spolecne", 20).lower()
    if person not in {"hanych", "eva", "spolecne"}:
        person = "spolecne"
    status = clean(payload.get("status") or "wanted", 20).lower()
    if status not in {"wanted", "bought", "archived"}:
        status = "wanted"
    notes = clean(payload.get("notes"), 1000)
    with db() as conn:
        if item_id:
            cur = conn.execute(
                """UPDATE wishlist SET title=?,url=?,price=?,priority=?,person=?,status=?,notes=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (title, url, price, priority, person, status, notes, item_id),
            )
            if not cur.rowcount:
                raise KeyError("Přání nebylo nalezeno.")
        else:
            cur = conn.execute(
                """INSERT INTO wishlist(title,url,price,priority,person,status,notes)
                   VALUES(?,?,?,?,?,?,?)""",
                (title, url, price, priority, person, status, notes),
            )
            item_id = cur.lastrowid
        conn.commit()
        return dict(conn.execute("SELECT * FROM wishlist WHERE id=?", (item_id,)).fetchone())


def delete_wishlist(item_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM wishlist WHERE id=?", (item_id,))
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Přání nebylo nalezeno.")


def _tesseract_tsv(path):
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "ces+eng", "--psm", "6", "tsv"],
            check=False, capture_output=True, timeout=60,
        )
    except FileNotFoundError:
        raise ValueError("OCR není v této instalaci dostupné.")
    if proc.returncode != 0:
        raise ValueError("Screenshot směn se nepodařilo přečíst.")
    rows = []
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    if not lines:
        return rows
    header = lines[0].split("\t")
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != len(header):
            continue
        item = dict(zip(header, parts))
        text = clean(item.get("text"), 120)
        if not text:
            continue
        try:
            item["left"] = int(item["left"]); item["top"] = int(item["top"])
            item["width"] = int(item["width"]); item["height"] = int(item["height"])
            item["conf"] = float(item["conf"])
        except (ValueError, KeyError):
            continue
        item["text"] = text
        rows.append(item)
    return rows


def _work_date_from_text(text, fallback_year):
    raw = clean(text, 80)
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        try:
            day, month, year = int(digits[:2]), int(digits[2:4]), int(digits[4:])
            return date(year, month, day)
        except ValueError:
            pass
    m = re.search(r"\b([0-3]?\d)\D+([01]?\d)\D+(20\d{2})\b", raw)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"\b([0-3]?\d)\D+([01]?\d)\b", raw)
    if m:
        try:
            return date(int(fallback_year), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    return None


def _work_name_score(text):
    compact = norm(text).replace(" ", "")
    if not compact:
        return 0.0
    score = SequenceMatcher(None, compact, "janvanek").ratio()
    if score >= 0.72:
        return score
    # OCR on tiny spreadsheet text often loses the first J or diacritics.
    if compact.startswith(("janvan", "ianvan", "lanvan", "anvan")):
        return max(score, 0.76)
    if 5 <= len(compact) <= 13 and "v" in compact and "n" in compact and score >= 0.55:
        return score
    return 0.0


def _work_crop_tsv(source, box, scale=5):
    crop = source.crop(box).convert("L")
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    crop = ImageOps.autocontrast(crop)
    crop = ImageEnhance.Contrast(crop).enhance(1.9)
    crop = crop.filter(ImageFilter.SHARPEN)
    with tempfile.TemporaryDirectory(prefix="haneva-shift-ocr-") as temp:
        target = Path(temp) / "crop.png"
        crop.save(target)
        rows = _tesseract_tsv(target)
    return rows, scale


def _scan_work_image(path, year_hint=None):
    source = Image.open(path)
    width, height = source.size
    if width < 500 or height < 180:
        raise ValueError("Screenshot směn je příliš malý.")

    # Layout is intentionally ratio-based so monthly screenshots can have
    # different pixel sizes while keeping the same Excel/Sheets structure.
    top = max(0, int(height * 0.045))
    bottom = min(height, int(height * 0.985))
    date_box = (0, top, max(90, int(width * 0.047)), bottom)
    morning_box = (int(width * 0.038), top, int(width * 0.56), bottom)
    afternoon_box = (int(width * 0.605), top, int(width * 0.865), bottom)

    date_words, date_scale = _work_crop_tsv(source, date_box, scale=5)
    fallback_year = int(year_hint) if year_hint else date.today().year
    date_hits = []
    month_year = []
    for word in date_words:
        parsed = _work_date_from_text(word["text"], fallback_year)
        if not parsed:
            continue
        global_y = top + (word["top"] + word["height"] / 2) / date_scale
        date_hits.append((parsed, global_y))
        month_year.append((parsed.month, parsed.year))

    if len(date_hits) < 5:
        raise ValueError("Ve screenshotu jsem nenašel dost datumů pro spolehlivé přiřazení směn.")

    date_hits.sort(key=lambda item: item[1])
    # The spreadsheet contains one visual row per calendar day. Estimate row
    # height from OCR positions, where missing OCR rows create 2x/3x gaps.
    y_values = [item[1] for item in date_hits]
    diffs = [b - a for a, b in zip(y_values, y_values[1:]) if b - a > 2]
    if not diffs:
        raise ValueError("Řádky směn se nepodařilo rozpoznat.")
    base_step = min(diffs)
    normalized_steps = []
    for diff in diffs:
        multiplier = max(1, round(diff / base_step))
        normalized_steps.append(diff / multiplier)
    row_step = median(normalized_steps)
    anchor_date, anchor_y = date_hits[0]
    # The first OCR date is usually day 1. Preserve its actual parsed day so
    # the algorithm also works for partial-month screenshots.
    anchor_day = anchor_date.day
    month, year = max(set(month_year), key=month_year.count)

    def day_for_y(global_y):
        day = anchor_day + round((global_y - anchor_y) / row_step)
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def name_hits(box, shift_type):
        words, scale = _work_crop_tsv(source, box, scale=5)
        by_line = {}
        for word in words:
            key = (word.get("block_num"), word.get("par_num"), word.get("line_num"))
            by_line.setdefault(key, []).append(word)
        hits = []
        for line in by_line.values():
            line.sort(key=lambda w: w["left"])
            best = None
            for index, word in enumerate(line):
                # Test one, two and three adjacent OCR tokens. This covers
                # both "JanVaněk" and split "Jan" + "Vaněk".
                for count in (1, 2, 3):
                    part = line[index:index + count]
                    if len(part) != count:
                        continue
                    text_value = " ".join(x["text"] for x in part)
                    score = _work_name_score(text_value)
                    if score <= 0:
                        continue
                    left = part[0]["left"]
                    right = part[-1]["left"] + part[-1]["width"]
                    candidate = {
                        "score": score,
                        "text": text_value,
                        "x": (left + right) / 2,
                        "y": word["top"] + word["height"] / 2,
                    }
                    if best is None or candidate["score"] > best["score"]:
                        best = candidate
            if not best:
                continue
            global_y = box[1] + best["y"] / scale
            shift_date = day_for_y(global_y)
            if not shift_date:
                continue
            hits.append({
                "date": shift_date.isoformat(),
                "type": shift_type,
                "confidence": round(min(0.99, max(0.6, best["score"])), 2),
                "source_text": clean(best["text"], 120),
            })
        return hits

    shifts = name_hits(morning_box, "dopolední") + name_hits(afternoon_box, "odpolední")
    # Keep the highest-confidence occurrence for each date/type.
    unique = {}
    for shift in shifts:
        key = (shift["date"], shift["type"])
        if key not in unique or shift["confidence"] > unique[key]["confidence"]:
            unique[key] = shift
    shifts = sorted(unique.values(), key=lambda x: (x["date"], x["type"]))
    if not shifts:
        raise ValueError("Jméno Jan Vaněk jsem v tabulce nenašel.")
    return shifts

def scan_work(body, filename, content_type="", year_hint=None):
    if not body or len(body) > MAX_UPLOAD:
        raise ValueError("Screenshot může mít nejvýše 18 MB.")
    mime, ext = _mime_from_body(body, filename, content_type)
    if mime == "application/pdf":
        raise ValueError("Pro směny nahraj screenshot JPG nebo PNG.")
    digest = hashlib.sha256(body).hexdigest()
    with tempfile.TemporaryDirectory(prefix="haneva-work-") as temp:
        path = Path(temp) / ("source" + ext)
        path.write_bytes(body)
        shifts = _scan_work_image(path, year_hint)
    with db() as conn:
        row = conn.execute("SELECT id FROM work_imports WHERE sha256=?", (digest,)).fetchone()
        if row:
            import_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO work_imports(sha256,original_name,raw_text) VALUES(?,?,?)",
                (digest, clean(filename, 180), json.dumps(shifts, ensure_ascii=False)),
            )
            import_id = cur.lastrowid
            conn.commit()
    return {"import_id": import_id, "shifts": shifts}


def commit_work_shifts(payload):
    import_id = payload.get("import_id")
    shifts = payload.get("shifts") or []
    added, skipped = [], []
    with sqlite3.connect(CALENDAR_DB_PATH, timeout=15) as cal:
        cal.row_factory = sqlite3.Row
        for shift in shifts:
            if not shift.get("selected", True):
                continue
            shift_date = clean(shift.get("date"), 10)
            shift_type = clean(shift.get("type"), 30).lower()
            if shift_type not in {"dopolední", "odpolední"}:
                continue
            try:
                date.fromisoformat(shift_date)
            except ValueError:
                continue
            with db() as conn:
                exists = conn.execute(
                    "SELECT id,event_id FROM work_shifts WHERE shift_date=? AND shift_type=?",
                    (shift_date, shift_type),
                ).fetchone()
            if exists:
                skipped.append({"date": shift_date, "type": shift_type})
                continue
            title = f"Práce · {shift_type}"
            notes = "Haneva: automaticky importovaná směna pro Jan Vaněk."
            start_time, end_time = ("08:00", "16:00") if shift_type == "dopolední" else ("14:00", "22:00")
            cur = cal.execute(
                """INSERT INTO events
                   (title,calendar,start_date,end_date,start_time,end_time,all_day,location,notes,recurrence,event_type)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (title, "hanych", shift_date, shift_date, start_time, end_time, 0, "", notes, "none", ""),
            )
            event_id = cur.lastrowid
            with db() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO work_shifts(shift_date,shift_type,event_id,import_id)
                       VALUES(?,?,?,?)""",
                    (shift_date, shift_type, event_id, import_id),
                )
                conn.commit()
            added.append({"date": shift_date, "type": shift_type, "event_id": event_id})
        cal.commit()
    return {"added": added, "skipped": skipped}


def work_state():
    with db() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM work_shifts ORDER BY shift_date DESC,id DESC LIMIT 80"
        ).fetchall()]
    return {"shifts": rows}


def save_plate(body, content_type=""):
    if not body or len(body) > 8 * 1024 * 1024:
        raise ValueError("Referenční fotka může mít nejvýše 8 MB.")
    mime, _ = _mime_from_body(body, "plate.png", content_type)
    if mime == "application/pdf":
        raise ValueError("Nahraj fotografii talíře, ne PDF.")
    Path(PLATE_PATH).write_bytes(body)
    return {"ok": True, "has_plate": True}


def _options():
    try:
        return json.loads(Path(OPTIONS_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def generate_recipe_image(recipe_id):
    with db() as conn:
        recipe = _recipe_row(conn, recipe_id)
    options = _options()
    api_key = clean(options.get("openai_api_key"), 500)
    if not api_key:
        raise ValueError("Pro AI fotografie nejdřív nastav OpenAI API key v konfiguraci add-onu Haneva Home.")
    model = clean(options.get("openai_image_model") or "gpt-image-2", 80)
    ingredients = ", ".join(i["name"] for i in recipe["ingredients"][:12])
    prompt = (
        f"Photorealistic appetizing food photography of {recipe['title']}. "
        f"Ingredients and visual cues: {ingredients}. "
        "Natural home dining light, realistic portion for two people, no text, no logos. "
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = 120
    if Path(PLATE_PATH).exists():
        prompt += "Use the uploaded plate as the serving plate and preserve its shape, color and pattern."
        with open(PLATE_PATH, "rb") as image_file:
            response = requests.post(
                "https://api.openai.com/v1/images/edits",
                headers=headers,
                data={"model": model, "prompt": prompt, "size": "1024x1024"},
                files={"image": ("plate.png", image_file, "image/png")},
                timeout=timeout,
            )
    else:
        response = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={**headers, "Content-Type": "application/json"},
            json={"model": model, "prompt": prompt, "size": "1024x1024"},
            timeout=timeout,
        )
    try:
        data = response.json()
    except Exception:
        data = {}
    if response.status_code >= 300:
        message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else ""
        raise ValueError(clean(message or "AI fotografii se nepodařilo vytvořit.", 300))
    entry = (data.get("data") or [{}])[0]
    raw = None
    if entry.get("b64_json"):
        raw = base64.b64decode(entry["b64_json"])
    elif entry.get("url"):
        img = requests.get(entry["url"], timeout=60)
        img.raise_for_status()
        raw = img.content
    if not raw:
        raise ValueError("AI nevrátila obrázek.")
    name = f"recipe-{recipe_id}-{uuid.uuid4().hex[:10]}.png"
    target = Path(RECIPE_IMAGE_DIR) / name
    target.write_bytes(raw)
    with db() as conn:
        old = conn.execute("SELECT image_name FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        conn.execute("UPDATE recipes SET image_name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (name, recipe_id))
        conn.commit()
    if old and old["image_name"]:
        (Path(RECIPE_IMAGE_DIR) / old["image_name"]).unlink(missing_ok=True)
    return {"image_url": f"/api/v1/recipes/{recipe_id}/image"}


def _find_numeric(obj, paths):
    for path in paths:
        value = obj
        good = True
        for key in path:
            if not isinstance(value, dict) or key not in value:
                good = False; break
            value = value[key]
        if good:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def _parse_date_any(value):
    text = clean(value, 40)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d. %m. %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            pass
    m = DATE_RE.search(text)
    if m:
        y = m.group(3) or str(date.today().year)
        if len(y) == 2:
            y = "20" + y
        try:
            return date(int(y), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    return None


def _obj_value(obj, candidates):
    if not isinstance(obj, dict):
        return None
    lowered = {norm(k).replace(" ", "_"): v for k, v in obj.items()}
    for key in candidates:
        if key in lowered:
            return lowered[key]
    return None


def collect_transactions(payload):
    results = []
    seen = set()
    def walk(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    amount_raw = _obj_value(item, {"amount","castka","cena","value","price"})
                    date_raw = _obj_value(item, {"date","datum","created_at","created","day"})
                    amount = _money(amount_raw)
                    dt = _parse_date_any(date_raw)
                    if amount is not None and dt:
                        category = clean(_obj_value(item, {"category","kategorie","type","typ"}) or "Ostatní", 100)
                        who = clean(_obj_value(item, {"person","who","kdo","payer","paid_by"}) or "", 60)
                        desc = clean(_obj_value(item, {"shop","obchod","description","popis","name","nazev","note","poznamka"}) or "", 180)
                        key = (dt.isoformat(), round(amount,2), category, desc)
                        if key not in seen:
                            seen.add(key)
                            results.append({"date": dt, "amount": abs(amount), "category": category, "who": who, "description": desc})
                walk(item, depth+1)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child, depth+1)
    walk(payload)
    return results


def _receipt_transactions():
    with db() as conn:
        rows = conn.execute(
            "SELECT purchased_at,total,store FROM receipts WHERE total IS NOT NULL AND purchased_at IS NOT NULL"
        ).fetchall()
    out = []
    for row in rows:
        dt = _parse_date_any(row["purchased_at"])
        if dt:
            out.append({"date": dt, "amount": abs(float(row["total"])), "category": "Jídlo / nákup", "who": "", "description": row["store"]})
    return out


def finance_analysis(payload):
    today = date.today()
    tx = collect_transactions(payload)
    source = "Rozpočet"
    if not tx:
        tx = _receipt_transactions()
        source = "Účtenky"
    # Deposits to the shared account are not household spending.
    spending = [
        x for x in tx
        if "platba na spolecny ucet" not in norm(x["category"])
        and "vklad" not in norm(x["category"])
    ]
    current = [x for x in spending if x["date"].year == today.year and x["date"].month == today.month]
    spent = sum(x["amount"] for x in current)
    days_in_month = (date(today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1, 1) - date(today.year, today.month, 1)).days
    prediction = spent / max(1, today.day) * days_in_month

    start7 = today - timedelta(days=6)
    prev_start = start7 - timedelta(days=7)
    last7 = sum(x["amount"] for x in spending if start7 <= x["date"] <= today)
    prev7 = sum(x["amount"] for x in spending if prev_start <= x["date"] < start7)

    annual = [x for x in spending if x["date"].year == today.year]
    months = []
    for month in range(1, 13):
        value = sum(x["amount"] for x in annual if x["date"].month == month)
        months.append({"month": month, "amount": round(value, 2)})
    annual_total = sum(x["amount"] for x in annual)

    anomalies = []
    by_category = {}
    for x in annual:
        by_category.setdefault(x["category"] or "Ostatní", []).append(x)
    for category, rows in by_category.items():
        amounts = [r["amount"] for r in rows]
        med = median(amounts) if amounts else 0
        for row in rows:
            if row["date"].month == today.month and row["amount"] >= max(1800, med * 2.5):
                anomalies.append({
                    "kind": "transaction",
                    "title": row["description"] or category,
                    "message": f"{row['amount']:.0f} Kč je výrazně nad obvyklou částkou v kategorii {category}.",
                    "amount": row["amount"],
                    "date": row["date"].isoformat(),
                })
        month_totals = []
        for month in range(1, today.month):
            month_totals.append(sum(r["amount"] for r in rows if r["date"].month == month))
        month_totals = [v for v in month_totals if v > 0]
        current_cat = sum(r["amount"] for r in rows if r["date"].month == today.month)
        if month_totals and current_cat > max(800, mean(month_totals) * 1.5):
            anomalies.append({
                "kind": "category",
                "title": category,
                "message": f"Tento měsíc {current_cat:.0f} Kč, běžně kolem {mean(month_totals):.0f} Kč.",
                "amount": current_cat,
                "date": today.isoformat(),
            })
    anomalies.sort(key=lambda x: x["amount"], reverse=True)
    anomalies = anomalies[:6]

    balance = _find_numeric(payload, [
        ("shared","joint","balance"),
        ("shared","balance"),
    ]) if isinstance(payload, dict) else None

    snapshot = {
        "date": today.isoformat(),
        "source": source,
        "transaction_count": len(spending),
        "month_spent": round(spent, 2),
        "month_prediction": round(prediction, 2),
        "week_spent": round(last7, 2),
        "previous_week_spent": round(prev7, 2),
        "annual_total": round(annual_total, 2),
        "months": months,
        "anomalies": anomalies,
        "balance": balance,
    }
    with db() as conn:
        conn.execute(
            """INSERT INTO finance_snapshots(snapshot_date,payload,balance,spent,created_at)
               VALUES(?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(snapshot_date) DO UPDATE SET payload=excluded.payload,balance=excluded.balance,
                  spent=excluded.spent,created_at=CURRENT_TIMESTAMP""",
            (today.isoformat(), json.dumps(payload, ensure_ascii=False)[:500000], balance, spent),
        )
        conn.commit()
    return snapshot


def page_bytes(path):
    return Path(path).read_bytes()


def _raw_upload(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length <= 0:
        raise ValueError("Vyber soubor.")
    if length > MAX_UPLOAD:
        raise ValueError("Soubor je příliš velký.")
    body = handler.rfile.read(length)
    if len(body) != length:
        raise ValueError("Soubor nebyl nahrán celý.")
    return body


def handle_get(handler, parsed):
    path = parsed.path
    if path in ("/jidlo", "/jidlo/"):
        handler.send_bytes(page_bytes(FOOD_HTML_PATH)); return True
    if path in ("/wishlist", "/wishlist/"):
        handler.send_bytes(page_bytes(WISHLIST_HTML_PATH)); return True
    if path in ("/prehled", "/prehled/"):
        handler.send_bytes(page_bytes(INSIGHTS_HTML_PATH)); return True
    if path == "/api/v1/food/state":
        from urllib.parse import parse_qs
        q = parse_qs(parsed.query)
        handler.send_json(list_recipes(q.get("person", [""])[0])); return True
    if path == "/api/v1/planned":
        handler.send_json(planned_state()); return True
    if path == "/api/v1/settings":
        handler.send_json(settings_state()); return True
    if path == "/api/v1/wishlist":
        handler.send_json(wishlist_state()); return True
    if path == "/api/v1/receipts":
        handler.send_json({"receipts": list_receipts()}); return True
    if path == "/api/v1/prices":
        handler.send_json({"products": price_history_state()}); return True
    if path == "/api/v1/work":
        handler.send_json(work_state()); return True
    image_match = re.fullmatch(r"/api/v1/recipes/(\d+)/image", path)
    if image_match:
        rid = int(image_match.group(1))
        with db() as conn:
            row = conn.execute("SELECT image_name FROM recipes WHERE id=?", (rid,)).fetchone()
        if not row or not row["image_name"]:
            handler.send_json({"error": "Obrázek nebyl nalezen."}, 404); return True
        target = Path(RECIPE_IMAGE_DIR) / row["image_name"]
        if not target.is_file():
            handler.send_json({"error": "Obrázek nebyl nalezen."}, 404); return True
        handler.send_bytes(target.read_bytes(), 200, "image/png"); return True
    return False


def handle_post(handler, parsed):
    path = parsed.path
    if path == "/api/v1/recipes":
        handler.send_json({"recipe": create_recipe(handler.read_json())}, 201); return True
    m = re.fullmatch(r"/api/v1/recipes/(\d+)/vote", path)
    if m:
        payload = handler.read_json()
        handler.send_json(vote_recipe(int(m.group(1)), clean(payload.get("person"), 20).lower(), clean(payload.get("vote"), 20).lower()))
        return True
    m = re.fullmatch(r"/api/v1/recipes/(\d+)/plan", path)
    if m:
        handler.send_json(plan_recipe(int(m.group(1)))); return True
    m = re.fullmatch(r"/api/v1/recipes/(\d+)/generate-image", path)
    if m:
        handler.send_json(generate_recipe_image(int(m.group(1)))); return True
    m = re.fullmatch(r"/api/v1/planned/(\d+)/cooked", path)
    if m:
        payload = handler.read_json()
        handler.send_json(mark_cooked(int(m.group(1)), bool(payload.get("cooked", True)))); return True
    if path == "/api/v1/shopping-preview":
        handler.send_json(shopping_preview()); return True
    if path == "/api/v1/shopping-commit":
        handler.send_json(commit_shopping(handler.read_json()), 201); return True
    if path == "/api/v1/wishlist":
        handler.send_json({"item": save_wishlist(handler.read_json())}, 201); return True
    if path == "/api/v1/receipts/upload":
        body = _raw_upload(handler)
        filename = handler.headers.get("X-Filename", "uctenka")
        result = process_receipt(body, filename, handler.headers.get("Content-Type", ""))
        handler.send_json({"receipt": result}, 201); return True
    if path == "/api/v1/work/scan":
        body = _raw_upload(handler)
        filename = handler.headers.get("X-Filename", "smeny.png")
        year_hint = handler.headers.get("X-Shift-Year")
        handler.send_json(scan_work(body, filename, handler.headers.get("Content-Type", ""), year_hint), 201); return True
    if path == "/api/v1/work/commit":
        handler.send_json(commit_work_shifts(handler.read_json()), 201); return True
    if path == "/api/v1/plate/upload":
        body = _raw_upload(handler)
        handler.send_json(save_plate(body, handler.headers.get("Content-Type", "")), 201); return True
    if path == "/api/v1/finance/analyse":
        handler.send_json(finance_analysis(handler.read_json())); return True
    return False


def handle_put(handler, parsed):
    path = parsed.path
    if path == "/api/v1/settings":
        handler.send_json(set_settings(handler.read_json())); return True
    m = re.fullmatch(r"/api/v1/recipes/(\d+)", path)
    if m:
        handler.send_json({"recipe": update_recipe(int(m.group(1)), handler.read_json())}); return True
    m = re.fullmatch(r"/api/v1/wishlist/(\d+)", path)
    if m:
        handler.send_json({"item": save_wishlist(handler.read_json(), int(m.group(1)))}); return True
    return False


def handle_delete(handler, parsed):
    path = parsed.path
    m = re.fullmatch(r"/api/v1/recipes/(\d+)", path)
    if m:
        delete_recipe(int(m.group(1))); handler.send_json({"ok": True}); return True
    m = re.fullmatch(r"/api/v1/planned/(\d+)", path)
    if m:
        delete_planned(int(m.group(1))); handler.send_json({"ok": True}); return True
    m = re.fullmatch(r"/api/v1/wishlist/(\d+)", path)
    if m:
        delete_wishlist(int(m.group(1))); handler.send_json({"ok": True}); return True
    return False
