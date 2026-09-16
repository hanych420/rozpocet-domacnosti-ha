from __future__ import annotations

"""Food-first filtering for structured Lidl offers.

Haneva still consumes FaserF/ha-lidl through Home Assistant. This layer only
filters the already-structured offer objects so the shopping page is useful for
food shopping instead of showing Lidl's full non-food catalogue.
"""

import hashlib
import json

import shopping_integrations_v100 as base

core = base.core
VERSION = "0.10.2"

NONFOOD_TERMS = (
    "naradi", "nastroj", "sroubov", "vrtak", "brus", "pilka", "kladiv",
    "kufrik", "magnet", "popisovac", "fixy", "papirensk", "plachta",
    "zehlick", "povleceni", "prosterad", "polstar", "deka", "rucnik",
    "bunda", "tricko", "mikina", "kalhot", "ponoz", "obuv", "boty",
    "svetelny retez", "led svet", "zarovk", "baterie", "nabijeck", "kabel",
    "panev", "hrnec", "nadobi", "pribor", "otvirak", "kuchynske potreby",
    "lazura", "barva", "lak ", "stetec", "zahrad", "dilna", "auto-moto",
    "jar ", "jar power", "cistici", "cistic", "praci", "avivaz", "tablety do mycky",
    "myci prostredek", "drogerie", "kosmet", "sampon", "sprchovy gel", "deodor",
    "toaletni papir", "ubrousk", "kapesnik", "pytle na odpad", "alobal",
    "hrack", "stavebnice", "skolni", "kancelar", "elektron", "spotrebic",
    "nabytek", "domacnost", "textil", "odev", "fashion", "home & garden",
    "household", "cleaning", "tools", "diy", "garden", "textile", "clothing",
    "electronics", "kitchenware", "cookware", "stationery", "toys",
)

FOOD_TERMS = (
    "potrav", "jidlo", "food", "grocery", "fresh", "cerstv",
    "ovoce", "ovoc", "fruit", "zelenin", "vegetable", "salat", "rajcat", "paprik",
    "okurk", "cibul", "mrkev", "brambor", "avokad", "banan", "jabl", "pomeranc",
    "mandar", "citron", "jahod", "malin", "boruv", "hroz",
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


def _norm_text(*parts):
    return core._norm(" ".join(core._clean(part) for part in parts if part))


def _is_food_offer(offer):
    name = core._clean(offer.get("title") or offer.get("name") or "")
    category = core._clean(offer.get("category") or "")
    brand = core._clean(offer.get("brand") or "")
    packaging = core._clean(offer.get("packaging") or "")
    text = _norm_text(name, category, brand, packaging)

    if any(term in text for term in NONFOOD_TERMS):
        return False
    if any(term in text for term in FOOD_TERMS):
        return True

    ppu = _norm_text(offer.get("price_per_unit") or "")
    if any(unit in ppu for unit in ("/kg", "/100 g", "/100g", "/l", "/100 ml", "/100ml")):
        return True
    return False


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

    for state in sensors:
        attrs = state.get("attributes") or {}
        for offer in attrs.get("discounts") or []:
            if not isinstance(offer, dict):
                continue
            raw_count += 1
            if not _is_food_offer(offer):
                if len(rejected) < 12:
                    rejected.append(
                        f"{core._clean(offer.get('title') or offer.get('name') or '?')} [{core._clean(offer.get('category') or '-')}]"
                    )
                continue

            name = core._clean(offer.get("title") or offer.get("name") or "")
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
    return count


base.VERSION = VERSION
base._sync_lidl_from_ha = _sync_lidl_from_ha

start_worker = base.start_worker
request_sync = base.request_sync
