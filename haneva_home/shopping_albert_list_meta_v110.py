"""Expose Albert loyalty/activation conditions on shopping-list deal matches."""

import shopping_list_v104 as listmod
import shopping_official_base as core
import shopping_albert_v110 as albert

_original_deal_payload = listmod._deal_payload


def _deal_payload_with_albert_meta(row, score=100, auto_matched=True):
    data = _original_deal_payload(row, score, auto_matched)
    if data.get("store") != "albert" or not data.get("deal_id"):
        return data
    albert._ensure_meta_schema()
    with core.db() as conn:
        meta = conn.execute(
            "SELECT app_required, activation_required, condition_text FROM albert_deal_meta WHERE deal_id=?",
            (data["deal_id"],),
        ).fetchone()
    if meta:
        data["app_required"] = bool(meta["app_required"])
        data["activation_required"] = bool(meta["activation_required"])
        data["condition_text"] = meta["condition_text"] or ""
    return data


listmod._deal_payload = _deal_payload_with_albert_meta
