from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
import json
import os
import re
import sqlite3
import unicodedata

import requests
from bs4 import BeautifulSoup

DB_PATH = "/data/shopping.db"
KUPI_SEARCH_URL = "https://www.kupi.cz/hledej"
CACHE_HOURS = 6
MAX_QUERIES = 16
MAX_RESULTS_PER_QUERY = 6
ALLOWED_STORES = {"any", "lidl", "albert"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Initial profile inferred from the Lidl receipts supplied by the household.
# The score is only a starting priority for Kupi searches; normal Haneva usage
# keeps building the real history afterwards. INSERT OR IGNORE makes this seed
# one-time, so a user can later unwatch an item without it returning on restart.
RECEIPT_PROFILE = [
    ("Chléb", 14),
    ("Kuřecí prsa", 13),
    ("Kuřecí šunka", 12),
    ("Eidam", 11),
    ("Avokádo", 11),
    ("Tortilla wraps", 10),
    ("Ovesné vločky", 10),
    ("Jadel kořeněný", 10),
    ("Vepřová krkovice", 10),
    ("Kuřecí paličky", 10),
    ("Barilla těstoviny", 10),
    ("Banány", 9),
    ("Salát ledový", 9),
    ("Okurka", 9),
    ("Rajčata", 9),
    ("Cibule", 8),
    ("Mrkev", 8),
    ("Pistácie", 8),
    ("Tuňák", 8),
    ("Vejce", 8),
    ("Ovesný nápoj", 7),
    ("Bezlaktózové mléko", 7),
    ("Bílý jogurt bez laktózy", 7),
    ("Proteinový rohlík", 7),
    ("Rýže", 6),
]


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_name(value):
    value = str(value or "").strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"\s+", " ", value)
    return value[:160]


def seed_receipt_profile(conn):
    for display_name, score in RECEIPT_PROFILE:
        norm = normalize_name(display_name)
        conn.execute('''
            INSERT OR IGNORE INTO history
            (name_norm, display_name, times_added, times_completed, watched, last_added_at, last_completed_at)
            VALUES (?, ?, 0, ?, 1, NULL, NULL)
        ''', (norm, display_name, score))


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_norm TEXT NOT NULL,
                quantity TEXT NOT NULL DEFAULT '',
                preferred_store TEXT NOT NULL DEFAULT 'any',
                checked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                checked_at TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS history (
                name_norm TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                times_added INTEGER NOT NULL DEFAULT 0,
                times_completed INTEGER NOT NULL DEFAULT 0,
                watched INTEGER NOT NULL DEFAULT 0,
                last_added_at TEXT,
                last_completed_at TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS kupi_cache (
                query_norm TEXT PRIMARY KEY,
                query_text TEXT NOT NULL,
                payload TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
        ''')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_checked ON items(checked, created_at)")
        seed_receipt_profile(conn)
        conn.commit()


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _store_key(name):
    low = _clean(name).lower()
    if low.startswith("lidl"):
        return "lidl"
    if low.startswith("albert"):
        return "albert"
    return None


def _item_dict(row):
    item = dict(row)
    item["checked"] = bool(item["checked"])
    return item


def _upsert_history(conn, name, increment_added=0):
    norm = normalize_name(name)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('''
        INSERT INTO history(name_norm, display_name, times_added, last_added_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(name_norm) DO UPDATE SET
            display_name=excluded.display_name,
            times_added=history.times_added + ?,
            last_added_at=excluded.last_added_at
    ''', (norm, name.strip()[:160], increment_added, now if increment_added else None, increment_added))
    return norm


def get_state():
    with db() as conn:
        items = [_item_dict(row) for row in conn.execute('''
            SELECT * FROM items
            ORDER BY checked ASC,
                     CASE preferred_store WHEN 'lidl' THEN 1 WHEN 'albert' THEN 2 ELSE 3 END,
                     created_at ASC
        ''').fetchall()]
        history = [dict(row) for row in conn.execute('''
            SELECT display_name, name_norm, times_added, times_completed, watched,
                   last_added_at, last_completed_at
            FROM history
            WHERE watched=1 OR times_completed>0
            ORDER BY watched DESC, times_completed DESC, times_added DESC, display_name
            LIMIT 30
        ''').fetchall()]
    for row in history:
        row["watched"] = bool(row["watched"])
    return {"items": items, "history": history}


def add_item(payload):
    name = _clean(payload.get("name"))
    if not name:
        raise ValueError("Název položky je povinný.")
    quantity = _clean(payload.get("quantity"))[:80]
    store = _clean(payload.get("preferred_store") or "any").lower()
    if store not in ALLOWED_STORES:
        store = "any"
    norm = normalize_name(name)
    with db() as conn:
        existing = conn.execute('''
            SELECT * FROM items
            WHERE checked=0 AND name_norm=? AND preferred_store=?
            ORDER BY id LIMIT 1
        ''', (norm, store)).fetchone()
        if existing:
            if quantity:
                conn.execute("UPDATE items SET quantity=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (quantity, existing["id"]))
            _upsert_history(conn, name, 1)
            conn.commit()
            row = conn.execute("SELECT * FROM items WHERE id=?", (existing["id"],)).fetchone()
            return _item_dict(row)
        cur = conn.execute('''
            INSERT INTO items(name, name_norm, quantity, preferred_store)
            VALUES(?, ?, ?, ?)
        ''', (name[:160], norm, quantity, store))
        _upsert_history(conn, name, 1)
        conn.commit()
        row = conn.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone()
        return _item_dict(row)


def update_item(item_id, payload):
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise KeyError("Položka nebyla nalezena.")
        name = _clean(payload.get("name", row["name"]))[:160]
        if not name:
            raise ValueError("Název položky je povinný.")
        quantity = _clean(payload.get("quantity", row["quantity"]))[:80]
        store = _clean(payload.get("preferred_store", row["preferred_store"])).lower()
        if store not in ALLOWED_STORES:
            store = row["preferred_store"]
        checked = bool(payload.get("checked", bool(row["checked"])))
        was_checked = bool(row["checked"])
        checked_at = row["checked_at"]
        now = datetime.now(timezone.utc).isoformat()
        norm = normalize_name(name)
        if checked and not was_checked:
            checked_at = now
            conn.execute('''
                INSERT INTO history(name_norm, display_name, times_completed, last_completed_at)
                VALUES(?, ?, 1, ?)
                ON CONFLICT(name_norm) DO UPDATE SET
                    display_name=excluded.display_name,
                    times_completed=history.times_completed + 1,
                    last_completed_at=excluded.last_completed_at
            ''', (norm, name, now))
        elif not checked:
            checked_at = None
        conn.execute('''
            UPDATE items
            SET name=?, name_norm=?, quantity=?, preferred_store=?, checked=?,
                checked_at=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (name, norm, quantity, store, 1 if checked else 0, checked_at, item_id))
        conn.commit()
        return _item_dict(conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())


def delete_item(item_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise KeyError("Položka nebyla nalezena.")


def clear_completed():
    with db() as conn:
        cur = conn.execute("DELETE FROM items WHERE checked=1")
        conn.commit()
        return cur.rowcount


def set_watched(payload):
    name = _clean(payload.get("name"))
    if not name:
        raise ValueError("Název položky je povinný.")
    watched = bool(payload.get("watched", True))
    norm = normalize_name(name)
    with db() as conn:
        conn.execute('''
            INSERT INTO history(name_norm, display_name, watched)
            VALUES(?, ?, ?)
            ON CONFLICT(name_norm) DO UPDATE SET
                display_name=excluded.display_name,
                watched=excluded.watched
        ''', (norm, name[:160], 1 if watched else 0))
        conn.commit()
    return {"name": name[:160], "name_norm": norm, "watched": watched}


def _candidate_queries():
    result, seen = [], set()
    with db() as conn:
        active = conn.execute("SELECT name FROM items WHERE checked=0 ORDER BY created_at DESC LIMIT 20").fetchall()
        history = conn.execute('''
            SELECT display_name FROM history
            WHERE watched=1 OR times_completed>=2
            ORDER BY watched DESC, times_completed DESC, times_added DESC
            LIMIT 30
        ''').fetchall()
    for row in list(active) + list(history):
        name = _clean(row[0])
        norm = normalize_name(name)
        if name and norm not in seen:
            seen.add(norm)
            result.append(name)
        if len(result) >= MAX_QUERIES:
            break
    return result


def _cached(query):
    norm = normalize_name(query)
    with db() as conn:
        row = conn.execute("SELECT payload, fetched_at FROM kupi_cache WHERE query_norm=?", (norm,)).fetchone()
    if not row:
        return None
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
        payload = json.loads(row["payload"])
    except Exception:
        return None
    return {"payload": payload, "fetched_at": fetched}


def _save_cache(query, payload):
    norm = normalize_name(query)
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute('''
            INSERT INTO kupi_cache(query_norm, query_text, payload, fetched_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(query_norm) DO UPDATE SET
                query_text=excluded.query_text,
                payload=excluded.payload,
                fetched_at=excluded.fetched_at
        ''', (norm, query, json.dumps(payload, ensure_ascii=False), now))
        conn.commit()
    return now


def _scrape_query(query):
    response = requests.get(
        KUPI_SEARCH_URL,
        params={"f": query, "vse": "0"},
        headers={"User-Agent": USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.5"},
        timeout=15,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    results = []
    for product in soup.find_all("div", class_="group_discounts"):
        name_tag = product.find("div", class_="product_name")
        strong = name_tag.find("strong") if name_tag else None
        if not strong:
            continue
        product_name = _clean(strong.get_text(" ", strip=True))
        table = product.find("div", class_="discounts_table")
        if not table:
            continue
        shops = table.find_all("span", class_="discounts_shop_name")
        rows = table.find_all("div", class_="discount_row")
        for idx, discount_row in enumerate(rows):
            shop_name = _clean(shops[idx].get_text(" ", strip=True)) if idx < len(shops) else ""
            shop = _store_key(shop_name)
            if not shop:
                continue
            price_el = discount_row.find(class_="discount_price_value")
            amount_el = discount_row.find(class_="discount_amount")
            validity_el = discount_row.find("div", class_="discounts_validity")
            price = _clean(price_el.get_text(" ", strip=True) if price_el else "")
            amount = _clean(amount_el.get_text(" ", strip=True) if amount_el else "")
            validity = _clean(validity_el.get_text(" ", strip=True) if validity_el else "")
            if not price:
                continue
            results.append({
                "query": query,
                "name": product_name,
                "shop": shop,
                "shop_name": "Lidl" if shop == "lidl" else "Albert",
                "price": price,
                "amount": amount,
                "validity": validity,
                "kupi_url": f"https://www.kupi.cz/hledej?f={quote_plus(query)}&vse=0",
            })
            if len(results) >= MAX_RESULTS_PER_QUERY:
                return results
    return results


def _load_query(query, force=False):
    cached = _cached(query)
    now = datetime.now(timezone.utc)
    if cached and not force and now - cached["fetched_at"] < timedelta(hours=CACHE_HOURS):
        return {"query": query, "deals": cached["payload"], "fetched_at": cached["fetched_at"].isoformat(), "cached": True, "error": None}
    try:
        deals = _scrape_query(query)
        fetched_at = _save_cache(query, deals)
        return {"query": query, "deals": deals, "fetched_at": fetched_at, "cached": False, "error": None}
    except Exception as exc:
        if cached:
            return {"query": query, "deals": cached["payload"], "fetched_at": cached["fetched_at"].isoformat(), "cached": True, "error": f"Kupi se nepodařilo obnovit: {exc}"}
        return {"query": query, "deals": [], "fetched_at": None, "cached": False, "error": f"Kupi se nepodařilo načíst: {exc}"}


def get_deals(force=False):
    queries = _candidate_queries()
    if not queries:
        return {"deals": [], "queries": [], "updated_at": None, "errors": [], "source": "Kupi.cz"}
    parts = []
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
        futures = {pool.submit(_load_query, query, force): query for query in queries}
        for future in as_completed(futures):
            parts.append(future.result())
    deals, seen, errors = [], set(), []
    updated_at = None
    query_order = {normalize_name(q): i for i, q in enumerate(queries)}
    parts.sort(key=lambda p: query_order.get(normalize_name(p["query"]), 999))
    for part in parts:
        if part["error"]:
            errors.append({"query": part["query"], "message": part["error"]})
        if part["fetched_at"] and (updated_at is None or part["fetched_at"] > updated_at):
            updated_at = part["fetched_at"]
        for deal in part["deals"]:
            key = (normalize_name(deal.get("name")), deal.get("shop"), deal.get("price"), deal.get("amount"))
            if key in seen:
                continue
            seen.add(key)
            deals.append(deal)
    return {"deals": deals, "queries": queries, "updated_at": updated_at, "errors": errors, "source": "Kupi.cz"}
