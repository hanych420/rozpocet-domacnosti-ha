from __future__ import annotations

"""Food-first filtering and grouping for structured Lidl offers.

Haneva consumes FaserF/ha-lidl through Home Assistant. This layer filters the
already-structured offer objects so the shopping page focuses on groceries,
fixes a few semantic groups, and logs enough detail to diagnose Lidl Plus-only
promotions that are absent from the public Offers sensors.
"""

import hashlib
import json

import shopping_integrations_v100 as base

core = base.core
VERSION = "0.10.3"

NONFOOD_TERMS = (
    "naradi", "nastroj", "sroubov", "vrtak", "brus", "pilka", "kladiv",
    "kufrik", "magnet", "popisovac", "fixy", "papirensk", "plachta",
    "zehlick", "povleceni", "prosterad", "polstar", "deka", "rucnik",
    "bunda", "tricko", "mikina", "kalhot", "ponoz", "obuv", "boty",
    "svetelny retez", "led svet", "zarovk", "baterie", "nabijeck", "kabel",
    "panev", "hrnec", "nadobi", "pribor", "otvirak", "kuchynske potreby",
    "servirovaci", "insulated food container", "food container", "storage container",
    "termo nadob", "doza na potrav", "lazura", "akvarel", "olejove barv",
    "malirs", "vytvarn", "barvy", "barva ", "lak ", "stetec", "zahrad", "dilna",
    "auto-moto", "jar ", "jar power", "cistici", "cistic", "praci", "avivaz",
    "tablety do mycky", "myci prostredek", "drogerie", "kosmet", "sampon",
    "sprchovy gel", "deodor", "toaletni papir", "ubrousk", "kapesnik",
    "pytle na odpad", "alobal", "hrack", "stavebnice", "skolni", "kancelar",
    "elektron", "spotrebic", "nabytek", "domacnost", "textil", "odev", "fashion",
    "home & garden", "household", "cleaning", "tools", "diy", "garden", "textile",
    "clothing", "electronics", "kitchenware", "cookware", "stationery", "toys",
    "art supplies", "arts & crafts", "creative supplies",
)

FOOD_TERMS = (
    "potrav", "jidlo", "food", "grocery", "fresh", "cerstv",
    "ovoce", "ovoc", "fruit", "zelenin", "vegetable", "salat", "rajcat", "paprik",
    "okurk", "cibul", "mrkev", "brambor", "avokad", "banan", "jabl", "pomeranc",
    "mandar", "citron", "jahod", "malin", "boruv", "hroz", "grape",
    "maso", "meat", "kurec", "veprov", "hovez", "kruti", "kachn", "ryba", "fish",
    "losos", "tunak", "sunka", "salam", "klobas", "uzenin", "slanina",
    "mlec", "dairy", "mleko", "jogurt", "syr", "eidam", "gouda", "maslo", "tvaroh",
    "vejce", "egg", "pecivo", "bakery", "chleb", "rohlik", "housk", "baget",
    "testovin", "pasta", "ryze", "rice", "mouka", "cukr", "sul ", "olej", "ocet",
    "konzerv", "plechov", "omack", "kecup", "horcic", "majonez", "lustenin",
    "snack", "chips", "cokolad", "susenk", "oplat", "bonbon", "sladkost", "confection",
    "zmrzlin", "frozen", "mrazen", "pizza", "hotove jidlo", "ready meal",
    "napoj", "beverage", "drink", "voda", "dzus", "limonad", "cola", "kava", "coffee",
    "caj", "tea", "pivo", "beer", "vino", "wine", "alkohol", "spirit",
    "snidane", "breakfast", "cereal", "musli", "vlock", "presnidav", "detska vyziva",
    "bio", "vegan", "vegetarian", "delikates", "deli", "chlazene", "chilled",
)

# If one of these is in the actual product name, it is clearly grocery food and
# should not get lost because of an unfortunate category/packaging word.
STRONG_FOOD_NAME_TERMS = (
    "hroz", "grape", "presnidav", "detska vyziva", "jogurt", "mleko", "syr",
    "kurec", "veprov", "hovez", "rajcat", "banan", "jabl", "jahod", "malin",
)


def _norm_text(*parts):
    return core._norm(" ".join(core._clean(part) for part in parts if part))


def _is_food_offer(offer):
    name = core._clean(offer.get("title") or offer.get("name") or "")
    category = core._clean(offer.get("category") or "")
    brand = core._clean(offer.get("brand") or "")
    packaging = core._clean(offer.get("packaging") or "")
    name_norm = _norm_text(name)
    text = _norm_text(name, category, brand, packaging)

    if any(term in name_norm for term in STRONG_FOOD_NAME_TERMS):
        return True
    if any(term in text for term in NONFOOD_TERMS):
        return False
    if any(term in text for term in FOOD_TERMS):
        return True

    ppu = _norm_text(offer.get("price_per_unit") or "")
    if any(unit in ppu for unit in ("/kg", "/100 g", "/100g", "/l", "/100 ml", "/100ml")):
        return True
    return False


# Semantic grouping corrections. The legacy grouper sees words such as
# "ovocná", "jahody" or "maliny" and would otherwise put a fruit puree under
# generic Ovoce. Specific product types must win before generic ingredients.
_original_group_for = core._group_for


def _group_for_v103(name):
    n = core._norm(name)
    if "presnidav" in n or "detska vyziva" in n:
        return "presnidavky", "Přesnídávky a dětská výživa"
    if "hroz" in n or "grape" in n:
        return "ovoce", "Ovoce"
    return _original_group_for(name)


core._group_for = _group_for_v103


def _coupon_sensor_stats(states):
    sensors = 0
    coupons = 0
    names = []
    for state in states:
        if not isinstance(state, dict):
            continue
        attrs = state.get("attributes") or {}
        if not isinstance(attrs, dict) or not isinstance(attrs.get("coupons"), list):
            continue
        entity_id = str(state.get("entity_id") or "")
        if not entity_id.startswith("sensor."):
            continue
        sensors += 1
        coupons += len(attrs.get("coupons") or [])
        if len(names) < 4:
            names.append(entity_id)
    return sensors, coupons, names


def _sync_lidl_from_ha():
    states = base._ha_states()
    sensors = base._lidl_sensor_states(states)
    if not sensors:
        raise RuntimeError(
            "nenašel jsem senzory Lidl Weekly Offers v Home Assistantu; nainstaluj a nastav integraci FaserF/ha-lidl pro CZ"
        )

    deals = []
    fingerprint_rows = []
    rejected = []
    raw_count = 0
    grape_raw = []

    for state in sensors:
        attrs = state.get("attributes") or {}
        entity_id = str(state.get("entity_id") or "")
        for offer in attrs.get("discounts") or []:
            if not isinstance(offer, dict):
                continue
            raw_count += 1
            raw_name = core._clean(offer.get("title") or offer.get("name") or "")
            raw_name_norm = _norm_text(raw_name)
            is_grape = "hroz" in raw_name_norm or "grape" in raw_name_norm
            keep = _is_food_offer(offer)
            if is_grape:
                grape_raw.append(
                    f"{raw_name or '?'} [{core._clean(offer.get('category') or '-')}] {offer.get('start_date') or '?'}..{offer.get('end_date') or '?'} keep={keep} sensor={entity_id}"
                )
            if not keep:
                if len(rejected) < 12:
                    rejected.append(
                        f"{raw_name or '?'} [{core._clean(offer.get('category') or '-')}]"
                    )
                continue

            name = raw_name
            current = core._float_price(offer.get("price"))
            if not name or current is None:
                continue

            old = core._float_price(offer.get("old_price"))
            discount_text = core._clean(offer.get("discount"))
            deals.append({
                "name": name,
                "price_value": current,
                "original_price_value": old,
                "discount_percent": core._discount_percent(current, old, discount_text),
                "discount_text": discount_text,
                "amount": core._clean(offer.get("packaging") or offer.get("price_per_unit") or ""),
                "valid_from": offer.get("start_date"),
                "valid_to": offer.get("end_date"),
                "source_url": "",
                "category": core._clean(offer.get("category") or ""),
                "brand": core._clean(offer.get("brand") or ""),
            })
            fingerprint_rows.append({
                "id": offer.get("id"),
                "title": name,
                "category": offer.get("category"),
                "price": offer.get("price"),
                "old_price": offer.get("old_price"),
                "start": offer.get("start_date"),
                "end": offer.get("end_date"),
            })

    if not deals:
        raise RuntimeError(
            f"Lidl integrace vrátila {raw_count} nabídek, ale po potravinovém filtru nezůstala žádná"
        )

    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    count = base._replace_store("lidl", deals, fingerprint)
    base._log(
        f"Lidl food-only: {count}/{raw_count} nabídek ponecháno, {raw_count - len(deals)} ne-potravinových položek odfiltrováno; senzory={len(sensors)}"
    )
    if rejected:
        base._log("Lidl food-only rejected samples: " + " | ".join(rejected))
    if grape_raw:
        base._log("Lidl hrozny v Offers senzorech: " + " | ".join(grape_raw[:8]))
    else:
        coupon_sensors, coupon_count, coupon_names = _coupon_sensor_stats(states)
        suffix = f"; coupon sensors={coupon_sensors}, coupons={coupon_count}"
        if coupon_names:
            suffix += f" ({', '.join(coupon_names)})"
        base._log("Lidl hrozny: v Offers ani Offers Preview je HA integrace neposlala" + suffix)
    return count


base.VERSION = VERSION
base._sync_lidl_from_ha = _sync_lidl_from_ha

start_worker = base.start_worker
request_sync = base.request_sync
