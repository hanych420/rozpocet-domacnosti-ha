from __future__ import annotations

"""Managed Albert source for Haneva 0.11.0.

No PDF/HTML/OCR parsing is performed here. Haneva consumes structured JSON from
Parse.bot. The public Albert Parse API currently exposes leaflet metadata and OCR
pages; for full weekly products an optional custom structured endpoint can be
configured. Structured monthly-deal content is consumed directly when it exposes
product fields.
"""

from datetime import date
import hashlib
import json
import re

import shopping_integrations_v100 as source
import shopping_official_base as core

VERSION = "0.11.0"

_NAME_KEYS = (
    "productName", "product_name", "productTitle", "product_title", "name", "title", "headline"
)
_PRICE_KEYS = (
    "promoPrice", "promo_price", "salePrice", "sale_price", "discountPrice", "discount_price",
    "currentPrice", "current_price", "finalPrice", "final_price", "priceValue", "price_value", "price"
)
_OLD_PRICE_KEYS = (
    "oldPrice", "old_price", "originalPrice", "original_price", "regularPrice", "regular_price",
    "previousPrice", "previous_price", "priceBefore", "price_before"
)
_AMOUNT_KEYS = ("amount", "packaging", "unit", "size", "salesUnitSize", "sales_unit_size")
_DATE_FROM_KEYS = ("valid_from", "validFrom", "startDate", "start_date", "validityStartDate")
_DATE_TO_KEYS = ("valid_to", "validTo", "endDate", "end_date", "validityEndDate")
_TEXT_KEYS = ("text", "description", "subtitle", "label", "badge", "promotionText", "promotion_text")

_BAD_TITLES = (
    "usetrete", "ušetřete", "jak ziskate", "jak získáte", "muj albert", "můj albert",
    "hit mesice", "hit měsíce", "aplikace", "platnost akce", "supermarket", "hypermarket"
)


def _ensure_meta_schema():
    with core.db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS albert_deal_meta (
                deal_id INTEGER PRIMARY KEY,
                app_required INTEGER NOT NULL DEFAULT 0,
                activation_required INTEGER NOT NULL DEFAULT 0,
                condition_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()


def _scalar_text(obj):
    parts = []
    for key, value in obj.items():
        if key in {"imageUrl", "image_url", "url", "viewUrl", "view_url"}:
            continue
        if isinstance(value, (str, int, float)):
            parts.append(str(value))
    return " ".join(parts)


def _condition(text):
    raw = core._clean(text)
    n = core._norm(raw)
    activation = any(term in n for term in (
        "aktivuj", "aktivace", "aktivovat", "aktivujte", "kupon", "coupon"
    ))
    app = activation or any(term in n for term in (
        "muj albert", "s aplikaci", "v aplikaci", "karta z aplikace", "kartou muj albert",
        "nejnizsi cena s aplikaci", "cena s aplikaci"
    ))
    if activation:
        note = "Nutno aktivovat v aplikaci Můj Albert"
    elif app:
        note = "Cena platí s kartou Můj Albert"
    else:
        note = ""
    return app, activation, note


def _first(obj, keys):
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return None


def _price_from_structured_text(obj):
    """Fallback only for a structured CMS component, never OCR/page text."""
    for key in _TEXT_KEYS:
        value = obj.get(key)
        if not isinstance(value, str):
            continue
        match = re.search(r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:Kč|,-)(?!\d)", value, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _extract_structured(payload, defaults=None, source_url=""):
    defaults = defaults or {}
    products = []
    for obj in source._walk(payload):
        if not isinstance(obj, dict):
            continue
        name = _first(obj, _NAME_KEYS)
        if not isinstance(name, str):
            continue
        name = core._clean(name)
        name_norm = core._norm(name)
        if len(name) < 3 or len(name) > 180 or any(bad in name_norm for bad in _BAD_TITLES):
            continue

        price_raw = _first(obj, _PRICE_KEYS)
        if price_raw in (None, ""):
            # Parse.bot get_monthly_deals is structured CMS JSON; accept a price
            # only when it lives in the same component as a concrete product title.
            price_raw = _price_from_structured_text(obj)
        current = core._float_price(price_raw)
        if current is None or current <= 0:
            continue

        old = core._float_price(_first(obj, _OLD_PRICE_KEYS))
        context = _scalar_text(obj)
        app_required, activation_required, condition_text = _condition(context)
        valid_from = _first(obj, _DATE_FROM_KEYS) or defaults.get("valid_from")
        valid_to = _first(obj, _DATE_TO_KEYS) or defaults.get("valid_to")
        if not valid_from or not valid_to:
            try:
                parsed_from, parsed_to = core._date_range_from_text(context)
            except Exception:
                parsed_from, parsed_to = None, None
            valid_from = valid_from or parsed_from
            valid_to = valid_to or parsed_to

        products.append({
            "name": name,
            "price_value": current,
            "original_price_value": old,
            "discount_percent": core._discount_percent(
                current, old, obj.get("discount") or obj.get("discountPercent") or context
            ),
            "amount": core._clean(_first(obj, _AMOUNT_KEYS) or ""),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_url": source_url or defaults.get("view_url") or "",
            "app_required": app_required,
            "activation_required": activation_required,
            "condition_text": condition_text,
        })
    return products


def _replace_albert(deals, fingerprint):
    _ensure_meta_schema()
    cleaned = []
    seen = set()
    for deal in deals:
        name = core._clean(deal.get("name"))
        current = core._float_price(deal.get("price_value"))
        if not name or current is None or current <= 0:
            continue
        valid_from = core._iso_date(deal.get("valid_from"))
        valid_to = core._iso_date(deal.get("valid_to"))
        key = (core._norm(name), round(current, 2), valid_from or "", valid_to or "")
        if key in seen:
            continue
        seen.add(key)
        old = core._float_price(deal.get("original_price_value"))
        if old is not None and old <= current:
            old = None
        cleaned.append({
            **deal,
            "name": name[:180],
            "price_value": current,
            "price_text": core._fmt_price(current),
            "original_price_value": old,
            "original_price": core._fmt_price(old) if old else "",
            "valid_from": valid_from,
            "valid_to": valid_to,
        })

    if not cleaned:
        return 0

    marker = source.SOURCE_PREFIX + "albert:" + fingerprint
    now = source._now_iso()
    with core.db() as conn:
        old_ids = [r[0] for r in conn.execute("SELECT id FROM official_deals WHERE store='albert' AND source_kind='official'").fetchall()]
        if old_ids:
            placeholders = ",".join("?" for _ in old_ids)
            conn.execute(f"DELETE FROM albert_deal_meta WHERE deal_id IN ({placeholders})", old_ids)
        conn.execute("DELETE FROM official_deals WHERE store='albert' AND source_kind='official'")

        for deal in cleaned[:1800]:
            group_key, group_label = core._group_for(deal["name"])
            cur = conn.execute(
                """
                INSERT INTO official_deals(
                    store, name, name_norm, group_key, group_label,
                    price_text, price_value, original_price, original_price_value,
                    discount_percent, amount, valid_from, valid_to,
                    source_url, source_kind, source_fingerprint, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "albert", deal["name"], core._norm(deal["name"]), group_key, group_label,
                    deal["price_text"], deal["price_value"], deal.get("original_price") or "",
                    deal.get("original_price_value"), deal.get("discount_percent"),
                    core._clean(deal.get("amount"))[:100], deal.get("valid_from"), deal.get("valid_to"),
                    core._clean(deal.get("source_url"))[:1000], "official", marker, now,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO albert_deal_meta(deal_id, app_required, activation_required, condition_text) VALUES(?,?,?,?)",
                (
                    cur.lastrowid,
                    1 if deal.get("app_required") else 0,
                    1 if deal.get("activation_required") else 0,
                    core._clean(deal.get("condition_text"))[:250],
                ),
            )
        conn.commit()
    return len(cleaned)


def _existing_count():
    with core.db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM official_deals WHERE store='albert' AND source_kind='official'").fetchone()
    return int(row["n"] if row else 0)


def _sync_albert_parse_v110():
    opts = source._options()
    api_key = str(opts.get("parse_api_key") or "").strip()
    if not api_key:
        source._log("Albert: chybí Parse.bot API klíč; Haneva nepoužívá Kupi ani PDF/OCR parser")
        return 0

    current = source._parse_get("get_current_leaflets", api_key)
    leaflets = source._leaflets_from_response(current)
    if not leaflets:
        raise RuntimeError("Parse.bot nevrátil žádný aktuální Albert leták")

    deals = []
    custom_endpoint = core._clean(opts.get("parse_albert_products_endpoint") or "")
    if custom_endpoint:
        for leaflet in leaflets[:4]:
            payload = source._parse_get(custom_endpoint, api_key, {"id": leaflet["slug"]})
            deals.extend(_extract_structured(payload, leaflet, leaflet.get("view_url") or ""))

    # Managed structured CMS source for app/monthly offers. This is not OCR.
    try:
        monthly = source._parse_get("get_monthly_deals", api_key)
        deals.extend(_extract_structured(monthly, {}, "https://www.albert.cz/hit-mesice"))
    except Exception as exc:
        source._log(f"Albert get_monthly_deals: {type(exc).__name__}: {exc}")

    if not deals:
        existing = _existing_count()
        if existing:
            source._log(f"Albert: structured source returned no new rows, keeping {existing} existing offers")
            return existing
        source._log(
            "Albert: managed API is reachable, but full weekly leaflet products are still OCR-only. "
            "Set parse_albert_products_endpoint to a Parse.bot endpoint returning structured products."
        )
        return 0

    fingerprint = hashlib.sha256(
        json.dumps(deals, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    count = _replace_albert(deals, fingerprint)
    activation = sum(1 for d in deals if d.get("activation_required"))
    app_only = sum(1 for d in deals if d.get("app_required") and not d.get("activation_required"))
    source._log(f"Albert přes Parse.bot: {count} strukturovaných nabídek; aktivace={activation}, karta/aplikace={app_only}")
    return count


_original_serialize = core._serialize_row


def _serialize_row_with_albert_meta(row):
    data = _original_serialize(row)
    if data.get("store") != "albert" or not data.get("id"):
        return data
    _ensure_meta_schema()
    with core.db() as conn:
        meta = conn.execute(
            "SELECT app_required, activation_required, condition_text FROM albert_deal_meta WHERE deal_id=?",
            (data["id"],),
        ).fetchone()
    if meta:
        data["app_required"] = bool(meta["app_required"])
        data["activation_required"] = bool(meta["activation_required"])
        data["condition_text"] = meta["condition_text"] or ""
    return data


source.VERSION = VERSION
source._sync_albert_parse = _sync_albert_parse_v110
core._serialize_row = _serialize_row_with_albert_meta
_ensure_meta_schema()
