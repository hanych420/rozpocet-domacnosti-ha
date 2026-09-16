from __future__ import annotations

"""Shopping-list upgrades for Haneva 0.10.5.

Adds a numeric item count without breaking free-form quantity, increments exact
repeat additions, enriches list items with current deals, and lets a selected
deal attach to an already existing generic item (e.g. Hrozny -> red grapes)
instead of creating a duplicate row.
"""

from datetime import date
import re

import shopping
import shopping_official_base as core

VERSION = "0.10.5"

_original_init_db = shopping.init_db
_original_update_item = shopping.update_item
_original_item_dict = shopping._item_dict
_original_enrich_state = core.enrich_state


def _ensure_count_schema():
    with shopping.db() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "count" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN count INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        conn.execute("UPDATE items SET count=1 WHERE count IS NULL OR count < 1")
        conn.commit()


def init_db():
    _original_init_db()
    _ensure_count_schema()


def _item_dict(row):
    item = _original_item_dict(row)
    try:
        item["count"] = max(1, int(item.get("count") or 1))
    except Exception:
        item["count"] = 1
    return item


def _requested_count(payload, default=1):
    raw = payload.get("count", default)
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(1, min(value, 999))


def add_item(payload):
    _ensure_count_schema()
    name = shopping._clean(payload.get("name"))
    if not name:
        raise ValueError("Název položky je povinný.")
    quantity = shopping._clean(payload.get("quantity"))[:80]
    store = shopping._clean(payload.get("preferred_store") or "any").lower()
    if store not in shopping.ALLOWED_STORES:
        store = "any"
    norm = shopping.normalize_name(name)
    add_count = _requested_count(payload, 1)

    with shopping.db() as conn:
        existing = conn.execute(
            '''SELECT * FROM items
               WHERE checked=0 AND name_norm=?
               ORDER BY CASE WHEN preferred_store=? THEN 0 WHEN preferred_store='any' THEN 1 ELSE 2 END, id
               LIMIT 1''',
            (norm, store),
        ).fetchone()
        if existing:
            new_store = existing["preferred_store"]
            if new_store == "any" and store != "any":
                new_store = store
            new_quantity = quantity or existing["quantity"]
            current_count = max(1, int(existing["count"] or 1))
            conn.execute(
                "UPDATE items SET quantity=?, preferred_store=?, count=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_quantity, new_store, min(999, current_count + add_count), existing["id"]),
            )
            shopping._upsert_history(conn, name, 1)
            conn.commit()
            return _item_dict(conn.execute("SELECT * FROM items WHERE id=?", (existing["id"],)).fetchone())

        cur = conn.execute(
            '''INSERT INTO items(name, name_norm, quantity, preferred_store, count)
               VALUES(?, ?, ?, ?, ?)''',
            (name[:160], norm, quantity, store, add_count),
        )
        shopping._upsert_history(conn, name, 1)
        conn.commit()
        return _item_dict(conn.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone())


def update_item(item_id, payload):
    _ensure_count_schema()
    item = _original_update_item(item_id, payload)
    if "count" not in payload:
        return _item_dict_from_id(item_id) or item
    new_count = _requested_count(payload, item.get("count") or 1)
    with shopping.db() as conn:
        conn.execute(
            "UPDATE items SET count=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_count, item_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return _item_dict(row)


def _item_dict_from_id(item_id):
    with shopping.db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return _item_dict(row) if row else None


def _tokens(value):
    text = core._norm(value)
    return [token for token in re.findall(r"[a-z0-9]+", text) if len(token) >= 3]


def _token_match(a, b):
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return False


def _match_score(item_name, deal_name):
    item_norm = core._norm(item_name)
    deal_norm = core._norm(deal_name)
    if not item_norm or not deal_norm:
        return 0
    if item_norm == deal_norm:
        return 100
    if min(len(item_norm), len(deal_norm)) >= 4 and (item_norm in deal_norm or deal_norm in item_norm):
        return 94

    item_tokens = _tokens(item_name)
    deal_tokens = _tokens(deal_name)
    if not item_tokens or not deal_tokens:
        return 0
    matched = sum(1 for token in item_tokens if any(_token_match(token, other) for other in deal_tokens))
    if matched == len(item_tokens):
        return 92 if len(item_tokens) > 1 else 86
    ratio = matched / len(item_tokens)
    return 78 if matched >= 2 and ratio >= 0.66 else 0


def _current_deals():
    today = date.today().isoformat()
    with core.db() as conn:
        rows = conn.execute(
            '''SELECT id, store, name, group_key, group_label, price_text, price_value,
                      original_price, discount_percent, amount, valid_from, valid_to,
                      source_url, source_kind
               FROM official_deals
               WHERE (valid_from IS NULL OR valid_from <= ?)
                 AND (valid_to IS NULL OR valid_to >= ?)
               ORDER BY price_value ASC, name ASC''',
            (today, today),
        ).fetchall()
    return [dict(row) for row in rows]


def _deal_payload(row, score=100, auto_matched=True):
    status, message = core._timing(row.get("valid_from"), row.get("valid_to"))
    return {
        "deal_id": row.get("id"),
        "name": row.get("name"),
        "store": row.get("store"),
        "price": row.get("price_text") or core._fmt_price(row.get("price_value")),
        "original_price": row.get("original_price") or "",
        "discount_percent": row.get("discount_percent"),
        "amount": row.get("amount") or "",
        "valid_from": row.get("valid_from"),
        "valid_to": row.get("valid_to"),
        "source_url": row.get("source_url") or "",
        "source_kind": row.get("source_kind") or "official",
        "status": status,
        "message": message,
        "group_key": row.get("group_key"),
        "group_label": row.get("group_label"),
        "auto_matched": auto_matched,
        "match_score": score,
    }


def _best_current_deal(item, rows):
    preferred = str(item.get("preferred_store") or "any").lower()
    best = None
    best_score = 0
    for row in rows:
        if preferred in {"lidl", "albert"} and row.get("store") != preferred:
            continue
        score = _match_score(item.get("name") or "", row.get("name") or "")
        if score < 78:
            continue
        if best is None or score > best_score or (
            score == best_score and (row.get("price_value") or 10**9) < (best.get("price_value") or 10**9)
        ):
            best = row
            best_score = score
    return _deal_payload(best, best_score, True) if best else None


def _attached_deal_ids(item_ids):
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    with core.db() as conn:
        rows = conn.execute(
            f"SELECT item_id, deal_id FROM shopping_item_deals WHERE item_id IN ({placeholders}) AND deal_id IS NOT NULL",
            item_ids,
        ).fetchall()
    return {int(row["item_id"]): int(row["deal_id"]) for row in rows if row["deal_id"] is not None}


def enrich_state(state):
    enriched = _original_enrich_state(state)
    rows = _current_deals()
    by_id = {int(row["id"]): row for row in rows if row.get("id") is not None}
    item_ids = [int(item["id"]) for item in enriched.get("items", []) if item.get("id") is not None]
    attached = _attached_deal_ids(item_ids)

    for item in enriched.get("items", []):
        if item.get("checked"):
            continue
        selected_id = attached.get(int(item.get("id") or 0))
        if selected_id and selected_id in by_id:
            item["deal"] = _deal_payload(by_id[selected_id], 100, False)
            continue
        matched = _best_current_deal(item, rows)
        if matched:
            item["deal"] = matched
    return enriched


def add_or_attach_deal(payload):
    """Select a deal for an existing similar open item, or add a new row.

    Choosing a deal for generic `Hrozny` should enrich that row, not create a
    second `Červené stolní hrozny bezsemenné` row. Count is intentionally not
    incremented when a deal is merely selected for an existing item.
    """
    _ensure_count_schema()
    deal_name = shopping._clean(payload.get("name"))
    if not deal_name:
        raise ValueError("Název položky je povinný.")
    store = shopping._clean(payload.get("preferred_store") or payload.get("store") or "any").lower()
    if store not in shopping.ALLOWED_STORES:
        store = "any"
    amount = shopping._clean(payload.get("quantity") or payload.get("amount") or "")[:80]

    with shopping.db() as conn:
        rows = conn.execute("SELECT * FROM items WHERE checked=0 ORDER BY id").fetchall()
        best = None
        best_score = 0
        for row in rows:
            score = _match_score(row["name"], deal_name)
            if score > best_score:
                best = row
                best_score = score
        if best is not None and best_score >= 86:
            preferred = best["preferred_store"]
            if preferred == "any" and store in {"lidl", "albert"}:
                preferred = store
            quantity = best["quantity"] or amount
            conn.execute(
                "UPDATE items SET preferred_store=?, quantity=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (preferred, quantity, best["id"]),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM items WHERE id=?", (best["id"],)).fetchone()
            return _item_dict(row), True, best_score

    item = add_item({
        "name": deal_name,
        "quantity": amount,
        "preferred_store": store,
        "count": 1,
    })
    return item, False, 0


shopping.init_db = init_db
shopping._item_dict = _item_dict
shopping.add_item = add_item
shopping.update_item = update_item
core.enrich_state = enrich_state
