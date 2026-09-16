from __future__ import annotations

"""Beer packaging enrichment for Albert offers in Haneva 0.11.4.

Parse.bot already classifies products semantically. This layer stores the new
beer packaging fields locally, exposes them through the deals API and adds them
to local semantic search. No Parse.bot request is performed here.
"""

import json

import shopping_albert_runtime_v110 as runtime
import shopping_albert_v110 as albert
import shopping_integrations_v100 as source
import shopping_official_base as core

_original_extract = runtime._extract_structured_with_flags
_original_ensure_schema = runtime._ensure_semantic_schema
_original_serialize = core._serialize_row

# Force one normal refresh after installing 0.11.4 so the newly added packaging
# fields are populated even when the previous Albert fetch is still inside the
# 24-hour cache window.
runtime._SEMANTIC_VERSION = "2"

_ALLOWED_CONTAINERS = {"bottle", "can", "pet", "keg"}


def _clean_container(value):
    value = core._norm(value or "")
    return value if value in _ALLOWED_CONTAINERS else ""


def _pack_count(value):
    if value in (None, ""):
        return None
    try:
        number = int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 200 else None


def _packaging_candidates(payload):
    by_name = {}
    for obj in source._walk(payload):
        if not isinstance(obj, dict):
            continue
        name = runtime._first(obj, albert._NAME_KEYS)
        if not isinstance(name, str):
            continue
        name = core._clean(name)
        if not name:
            continue

        container = _clean_container(runtime._first(obj, ("container", "container_type", "containerType")))
        pack_count = _pack_count(runtime._first(obj, ("pack_count", "packCount", "units_per_pack", "unitsPerPack")))
        unit_volume = core._clean(runtime._first(obj, ("unit_volume", "unitVolume")) or "")[:40]
        total_volume = core._clean(runtime._first(obj, ("total_volume", "totalVolume")) or "")[:40]
        package = core._clean(
            runtime._first(obj, ("package", "packageSize", "package_size") + albert._AMOUNT_KEYS) or ""
        )[:100]

        if not any((container, pack_count, unit_volume, total_volume)):
            continue

        by_name.setdefault(core._norm(name), []).append({
            "container": container,
            "pack_count": pack_count,
            "unit_volume": unit_volume,
            "total_volume": total_volume,
            "package": package,
        })
    return by_name


def _extract_with_packaging(payload, defaults=None, source_url=""):
    deals = _original_extract(payload, defaults, source_url)
    candidates = _packaging_candidates(payload)
    for deal in deals:
        options = candidates.get(core._norm(deal.get("name")), [])
        if not options:
            continue
        amount_norm = core._norm(deal.get("amount") or "")
        chosen = None
        if amount_norm:
            chosen = next((item for item in options if core._norm(item.get("package") or "") == amount_norm), None)
        chosen = chosen or options[0]
        deal["container"] = chosen.get("container") or ""
        deal["pack_count"] = chosen.get("pack_count")
        deal["unit_volume"] = chosen.get("unit_volume") or ""
        deal["total_volume"] = chosen.get("total_volume") or ""
    return deals


def _ensure_packaging_schema():
    _original_ensure_schema()
    wanted = {
        "container": "TEXT NOT NULL DEFAULT ''",
        "pack_count": "INTEGER",
        "unit_volume": "TEXT NOT NULL DEFAULT ''",
        "total_volume": "TEXT NOT NULL DEFAULT ''",
    }
    with core.db() as conn:
        present = {row["name"] for row in conn.execute("PRAGMA table_info(albert_search_meta)").fetchall()}
        for column, definition in wanted.items():
            if column not in present:
                conn.execute(f"ALTER TABLE albert_search_meta ADD COLUMN {column} {definition}")
        conn.commit()


def _packaging_aliases(container, pack_count, unit_volume, total_volume):
    aliases = []
    if container == "bottle":
        aliases.extend(("bottle", "lahev", "lahve", "lahvac", "lahvove"))
    elif container == "can":
        aliases.extend(("can", "cans", "plech", "plechovka", "plechovky", "plechove"))
    elif container == "pet":
        aliases.extend(("pet", "pet lahev", "plastova lahev"))
    elif container == "keg":
        aliases.extend(("keg", "sud", "soudek"))

    if pack_count and pack_count > 1:
        aliases.extend((
            "multipack", "pack", "baleni", f"{pack_count}pack", f"{pack_count} pack", f"{pack_count} ks"
        ))
    if unit_volume:
        aliases.append(unit_volume)
    if total_volume:
        aliases.append(total_volume)
    return aliases


def _store_semantic_and_packaging_meta(deals):
    _ensure_packaging_schema()
    by_key = {}
    for deal in deals:
        key = runtime._deal_key(
            deal.get("name"), deal.get("price_value"), deal.get("valid_from"), deal.get("valid_to")
        )
        current = by_key.setdefault(key, {
            "category": "",
            "subcategory": "",
            "terms": [],
            "container": "",
            "pack_count": None,
            "unit_volume": "",
            "total_volume": "",
        })
        current["category"] = current["category"] or core._clean(deal.get("category"))
        current["subcategory"] = current["subcategory"] or core._clean(deal.get("subcategory"))
        current["container"] = current["container"] or _clean_container(deal.get("container"))
        current["pack_count"] = current["pack_count"] or _pack_count(deal.get("pack_count"))
        current["unit_volume"] = current["unit_volume"] or core._clean(deal.get("unit_volume"))[:40]
        current["total_volume"] = current["total_volume"] or core._clean(deal.get("total_volume"))[:40]
        existing_norm = {core._norm(item) for item in current["terms"]}
        for term in deal.get("search_terms") or []:
            text = core._clean(term)
            norm = core._norm(text)
            if text and norm not in existing_norm:
                current["terms"].append(text)
                existing_norm.add(norm)

    stored = 0
    with core.db() as conn:
        rows = conn.execute(
            "SELECT id, name, price_value, valid_from, valid_to FROM official_deals "
            "WHERE store='albert' AND source_kind='official'"
        ).fetchall()
        conn.execute("DELETE FROM albert_search_meta")
        for row in rows:
            meta = by_key.get(runtime._deal_key(
                row["name"], row["price_value"], row["valid_from"], row["valid_to"]
            ))
            if not meta:
                continue
            terms = [item for item in meta["terms"] if item]
            package_aliases = _packaging_aliases(
                meta["container"], meta["pack_count"], meta["unit_volume"], meta["total_volume"]
            )
            search_text = core._norm(" ".join([
                row["name"], meta["category"], meta["subcategory"], *terms, *package_aliases
            ]))
            conn.execute(
                """
                INSERT OR REPLACE INTO albert_search_meta(
                    deal_id, category, subcategory, search_terms, search_text,
                    container, pack_count, unit_volume, total_volume
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(row["id"]),
                    meta["category"][:80],
                    meta["subcategory"][:80],
                    json.dumps(terms, ensure_ascii=False),
                    search_text,
                    meta["container"],
                    meta["pack_count"],
                    meta["unit_volume"],
                    meta["total_volume"],
                ),
            )
            if meta["category"] or meta["subcategory"] or terms or meta["container"] or meta["pack_count"]:
                stored += 1
        conn.commit()
    return stored


def _serialize_with_packaging(row):
    data = _original_serialize(row)
    if data.get("store") != "albert" or not data.get("id"):
        return data
    _ensure_packaging_schema()
    with core.db() as conn:
        meta = conn.execute(
            """
            SELECT category, subcategory, search_terms,
                   container, pack_count, unit_volume, total_volume
            FROM albert_search_meta WHERE deal_id=?
            """,
            (data["id"],),
        ).fetchone()
    if not meta:
        return data

    data["category"] = meta["category"] or ""
    data["subcategory"] = meta["subcategory"] or ""
    try:
        data["search_terms"] = json.loads(meta["search_terms"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        data["search_terms"] = []
    data["container"] = meta["container"] or ""
    data["pack_count"] = meta["pack_count"]
    data["unit_volume"] = meta["unit_volume"] or ""
    data["total_volume"] = meta["total_volume"] or ""
    return data


runtime._extract_structured_with_flags = _extract_with_packaging
albert._extract_structured = _extract_with_packaging
runtime._ensure_semantic_schema = _ensure_packaging_schema
runtime._store_semantic_meta = _store_semantic_and_packaging_meta
core._serialize_row = _serialize_with_packaging
_ensure_packaging_schema()
