from __future__ import annotations

"""Recommendation-first ordering for Haneva shopping deals.

No AI or external popularity service is used. Ranking learns from the household's
existing shopping history and from concrete deal selections in Haneva, then uses
query relevance, discount and price as deterministic tie-breakers.
"""

from datetime import datetime, timezone
import math

import shopping
import shopping_list_v104 as listmod
import shopping_official_base as core

VERSION = "0.11.7"

_original_get_grouped_deals = core.get_grouped_deals
_original_attach_item = core.attach_item


def _ensure_schema():
    with core.db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deal_choice_history (
                name_norm TEXT NOT NULL,
                store TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL,
                selections INTEGER NOT NULL DEFAULT 0,
                last_selected_at TEXT,
                PRIMARY KEY(name_norm, store)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deal_choice_selections "
            "ON deal_choice_history(selections DESC, last_selected_at DESC)"
        )
        conn.commit()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def attach_item(item_id, payload):
    """Attach the chosen deal and remember that concrete product selection."""
    _original_attach_item(item_id, payload)

    name = core._clean(payload.get("name"))
    if not name:
        return
    store = core._clean(payload.get("preferred_store") or payload.get("store") or "").lower()
    if store not in {"lidl", "albert"}:
        store = ""

    _ensure_schema()
    norm = core._norm(name)
    with core.db() as conn:
        conn.execute(
            """
            INSERT INTO deal_choice_history(name_norm, store, display_name, selections, last_selected_at)
            VALUES(?, ?, ?, 1, ?)
            ON CONFLICT(name_norm, store) DO UPDATE SET
                display_name=excluded.display_name,
                selections=deal_choice_history.selections + 1,
                last_selected_at=excluded.last_selected_at
            """,
            (norm, store, name[:180], _now_iso()),
        )
        conn.commit()


def _recency_bonus(raw):
    if not raw:
        return 0.0
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        days = max(0, (datetime.now(timezone.utc) - stamp).days)
    except Exception:
        return 0.0

    if days <= 14:
        return 12.0
    if days <= 45:
        return 8.0
    if days <= 120:
        return 4.0
    if days <= 365:
        return 1.5
    return 0.0


def _load_preferences():
    history = []
    choices = []
    try:
        with shopping.db() as conn:
            history = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT display_name, times_added, times_completed, watched,
                           last_added_at, last_completed_at
                    FROM history
                    WHERE watched=1 OR times_added>0 OR times_completed>0
                    ORDER BY times_completed DESC, times_added DESC
                    LIMIT 160
                    """
                ).fetchall()
            ]
    except Exception:
        history = []

    try:
        _ensure_schema()
        with core.db() as conn:
            choices = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT display_name, store, selections, last_selected_at
                    FROM deal_choice_history
                    WHERE selections>0
                    ORDER BY selections DESC, last_selected_at DESC
                    LIMIT 160
                    """
                ).fetchall()
            ]
    except Exception:
        choices = []

    return history, choices


def _semantic_meta(deal_ids):
    ids = sorted({int(value) for value in deal_ids if value is not None})
    if not ids:
        return {}
    try:
        placeholders = ",".join("?" for _ in ids)
        with core.db() as conn:
            rows = conn.execute(
                f"""
                SELECT deal_id, category, subcategory, search_text
                FROM albert_search_meta
                WHERE deal_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        return {int(row["deal_id"]): dict(row) for row in rows}
    except Exception:
        return {}


def _personal_score(deal, history, choices):
    name = deal.get("name") or ""
    store = deal.get("store") or deal.get("shop") or ""
    best = 0.0

    # Concrete offer selections are the strongest signal. Repeatedly choosing
    # the same product quickly moves it to the top.
    for row in choices:
        score = listmod._match_score(row.get("display_name") or "", name)
        if score < 78:
            continue
        if row.get("store") and store and row.get("store") != store:
            store_factor = 0.90
        else:
            store_factor = 1.0
        selections = max(0, int(row.get("selections") or 0))
        strength = min(125.0, 30.0 + 24.0 * math.log2(selections + 1))
        value = strength * (score / 100.0) * store_factor
        value += _recency_bonus(row.get("last_selected_at"))
        best = max(best, value)

    # Generic household history (including the existing receipt profile) lets
    # "chléb", "kuřecí prsa", etc. influence concrete leaflet products.
    for row in history:
        score = listmod._match_score(row.get("display_name") or "", name)
        if score < 78:
            continue
        completed = max(0, int(row.get("times_completed") or 0))
        added = max(0, int(row.get("times_added") or 0))
        watched = 1 if row.get("watched") else 0
        strength = min(
            92.0,
            completed * 5.0 + added * 3.0 + watched * 4.0,
        )
        value = strength * (score / 100.0)
        value += max(
            _recency_bonus(row.get("last_completed_at")),
            _recency_bonus(row.get("last_added_at")) * 0.7,
        )
        best = max(best, value)

    return best


def _query_score(deal, query, semantic):
    q = core._norm(query)
    if not q:
        return 0.0

    name = core._norm(deal.get("name") or "")
    group_key = core._norm(deal.get("group_key") or "")
    group_label = core._norm(deal.get("group_label") or "")

    if name == q:
        return 120.0
    if name.startswith(q):
        return 88.0
    if q in name:
        return 70.0

    q_tokens = [token for token in q.split() if len(token) >= 2]
    if q_tokens and all(token in name for token in q_tokens):
        return 62.0
    if q == group_key or q == group_label:
        return 52.0
    if q in group_key or q in group_label:
        return 42.0

    meta = semantic.get(int(deal.get("id") or 0), {})
    category = core._norm(meta.get("category") or "")
    subcategory = core._norm(meta.get("subcategory") or "")
    search_text = core._norm(meta.get("search_text") or "")
    if q == subcategory or q == category:
        return 58.0
    if q and q in search_text:
        return 48.0

    # The row already passed Haneva's search filter, so retain a small base
    # relevance even when the match came from another semantic path.
    return 20.0


def _discount_score(deal):
    try:
        discount = float(deal.get("discount_percent") or 0)
    except Exception:
        discount = 0.0
    return max(0.0, min(discount, 60.0)) * 0.35


def _rank(deal, query, history, choices, semantic):
    return (
        _query_score(deal, query, semantic)
        + _personal_score(deal, history, choices)
        + _discount_score(deal)
    )


def get_grouped_deals(store="all", time_filter="current", query=""):
    data = _original_get_grouped_deals(store=store, time_filter=time_filter, query=query)
    groups = list(data.get("groups") or [])
    if not groups:
        return data

    history, choices = _load_preferences()
    deal_ids = [
        deal.get("id")
        for group in groups
        for deal in (group.get("deals") or [])
        if deal.get("id") is not None
    ]
    semantic = _semantic_meta(deal_ids)

    for group in groups:
        deals = list(group.get("deals") or [])
        for deal in deals:
            deal["_recommendation_score"] = round(
                _rank(deal, query, history, choices, semantic), 3
            )

        deals.sort(
            key=lambda deal: (
                -float(deal.get("_recommendation_score") or 0),
                core._float_price(deal.get("price")) or 10**9,
                core._norm(deal.get("name") or ""),
            )
        )
        group["deals"] = deals
        group["_recommendation_score"] = max(
            (float(deal.get("_recommendation_score") or 0) for deal in deals),
            default=0.0,
        )
        group["_best_discount"] = max(
            (
                float(deal.get("discount_percent") or 0)
                for deal in deals
                if deal.get("discount_percent") is not None
            ),
            default=0.0,
        )

    # No alphabetical-first ordering: strongest recommendation first. When
    # household history has no preference yet, more offers and better discounts
    # naturally float up; price and alphabet are only final tie-breakers.
    groups.sort(
        key=lambda group: (
            -float(group.get("_recommendation_score") or 0),
            -len(group.get("deals") or []),
            -float(group.get("_best_discount") or 0),
            core._float_price(group.get("from_price")) or 10**9,
            core._norm(group.get("label") or ""),
        )
    )

    for group in groups:
        group.pop("_recommendation_score", None)
        group.pop("_best_discount", None)
        for deal in group.get("deals") or []:
            deal.pop("_recommendation_score", None)

    data["groups"] = groups
    data["ranking"] = "household-popularity"
    return data


_ensure_schema()
