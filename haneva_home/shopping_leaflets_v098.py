from __future__ import annotations

import re

from bs4 import BeautifulSoup

import shopping_leaflets_v097 as v097

v096 = v097.v096
v095 = v097.v095
v094 = v097.v094
legacy = v097.legacy
core = v097.core

# Runtime 0.9.7 showed two concrete things:
# 1) Lidl discovery reaches 180 real campaign product URLs, but most Lidl food
#    product pages do not expose an explicit validity range. Treating "no date"
#    as invalid therefore turned a healthy discovery into a fallback.
# 2) Albert's PDF extraction still allowed quantity/reference-price strings to
#    reach SQLite as product names. Filter those at the final storage boundary,
#    independent of whichever PDF parsing helper produced them.

HARD_QUANTITY_NAME_RE = re.compile(
    r"^\s*[•·|:;,.\-–—]*\s*"
    r"\d+(?:[.,]\d+)?"
    r"(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:kg|g|l|ml|cl|ks|role|roli|d[aá]vk(?:a|y|u|ou))\b",
    re.IGNORECASE,
)
REFERENCE_PRICE_RE = re.compile(
    r"(?:\b(?:100\s*g|1\s*kg|100\s*ml|1\s*l)\s*=|\bza\s*(?:kg|l)\b|/\s*(?:kg|l)\b)",
    re.IGNORECASE,
)


def _log(message):
    print(f"[Haneva][Akce 0.9.8] {message}", flush=True)


def _hard_bad_albert_name(name):
    text = legacy._clean(name).strip(" \t|•·;:,.–—-=_*/()[]{}")
    if not text:
        return True
    if HARD_QUANTITY_NAME_RE.match(text):
        return True
    norm = legacy._norm(text)
    if norm in {
        "aplikace", "aplikace /", "bez aplikace", "bez aplikace /",
        "s aplikaci", "muj albert", "albert", "super cena", "cena za",
    }:
        return True
    # Defensive catch for pdftotext artefacts such as "170–180 G •" where
    # punctuation or a tiny suffix survives normalization.
    if re.match(
        r"^\d+(?:[.,]\d+)?(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?\s*"
        r"(?:kg|g|l|ml|cl|ks|role|roli|davka|davky)\b",
        norm,
        re.IGNORECASE,
    ):
        return True
    return False


# ---------- Lidl: accept current campaign products even without an explicit date ----------

def _non_reference_prices(product_text):
    values = []
    for match in core.PRICE_RE.finditer(product_text or ""):
        value = core._float_price(match.group(0))
        if value is None or not (0 < value < 100000):
            continue
        context = product_text[max(0, match.start() - 55): match.end() + 20]
        # "1 kg = 199,60 Kč" is a unit/reference price, not the product price.
        if REFERENCE_PRICE_RE.search(context):
            continue
        values.append(value)
    return values


def _enrich_lidl(deal, html, product_text):
    deal = dict(deal)
    current = core._float_price(deal.get("price_value") or deal.get("price_text"))
    if current is None:
        return deal

    original = core._float_price(deal.get("original_price_value") or deal.get("original_price"))
    if original is None or original <= current:
        original = v097._markup_old_price(html, current)
    if original is None:
        original = v097._visible_old_price(product_text, current)
    if original is None:
        original = v097._json_old_price(html, current)

    pct = v097._explicit_discount(product_text)
    if original is not None and original > current:
        deal["original_price_value"] = original
        deal["original_price"] = core._fmt_price(original)
        deal["discount_percent"] = int(round((original - current) / original * 100))
    elif pct:
        deal["discount_percent"] = pct
    return deal


def _choose_lidl_product(url, html):
    soup = BeautifulSoup(html, "html.parser")
    text = legacy._clean(soup.get_text(" ", strip=True))
    page_name = v096._page_name(soup)
    if not page_name:
        v096._STATS["no_name"] += 1
        return None

    product_text = v096._main_product_block(text, page_name)
    valid_from, valid_to = v096._local_validity(product_text)
    if not (valid_from or valid_to):
        # This is normal for many Lidl food rows (including ongoing price
        # campaigns). The URL itself came from a currently published food/
        # campaign page, so keep it and let the next sync remove it when Lidl
        # stops publishing it there.
        v096._STATS["no_local_date"] += 1
    elif not v096._date_in_storage_window(valid_from, valid_to):
        v096._STATS["outside_window"] += 1
        return None

    # Prefer structured data when it belongs to the actual page product.
    target = core._norm(page_name)
    for candidate in core._parse_page("lidl", url, html):
        name_norm = core._norm(candidate.get("name"))
        if not name_norm:
            continue
        if target == name_norm or target in name_norm or name_norm in target:
            deal = dict(candidate)
            deal["name"] = page_name
            # Do not inherit a validity range from recommendation cards.
            deal["valid_from"] = valid_from
            deal["valid_to"] = valid_to
            deal["source_url"] = url
            v096._STATS["accepted"] += 1
            return _enrich_lidl(deal, html, product_text)

    prices = _non_reference_prices(product_text)
    if not prices:
        v096._STATS["no_price"] += 1
        return None

    # With an old crossed-out price the current price is displayed after it.
    # Unit/reference prices have already been removed above.
    current = prices[-1]
    original = min((p for p in prices[:-1] if p > current + 0.01), default=None)
    amount_match = re.search(
        r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|cl|ks|bal\.?|balení)\b",
        product_text,
        re.IGNORECASE,
    )
    deal = {
        "name": page_name,
        "price_value": current,
        "price_text": core._fmt_price(current),
        "original_price_value": original,
        "original_price": core._fmt_price(original),
        "discount_percent": core._discount_percent(current, original, product_text),
        "amount": amount_match.group(0) if amount_match else "",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "source_url": url,
    }
    v096._STATS["accepted"] += 1
    return _enrich_lidl(deal, html, product_text)


v096._choose_lidl_product = _choose_lidl_product
_OLD_LIDL_SYNC = v096._sync_lidl


def _sync_lidl():
    count, errors = _OLD_LIDL_SYNC()
    _log(
        f"Lidl accepted {count} official deals; undated campaign products are allowed; "
        f"nonfatal fetch errors={len(errors)}"
    )
    # Individual product/campaign fetch errors are nonfatal once we have a
    # usable official set. Do not show the user a fallback warning in that case.
    return count, [] if count else errors


# ---------- Albert: enforce name sanity at SQLite replacement ----------

_BASE_REPLACE = legacy._replace_store_deals
_OLD_ALBERT_SYNC = v097._sync_albert
_ALBERT_STORAGE_STATS = {"input": 0, "kept": 0, "rejected": 0, "samples": []}


def _filtered_replace_store_deals(store, deals, fingerprint):
    if store != "albert":
        return _BASE_REPLACE(store, deals, fingerprint)

    deals = list(deals or [])
    _ALBERT_STORAGE_STATS["input"] = len(deals)
    _ALBERT_STORAGE_STATS["samples"] = []
    kept = []
    rejected = 0
    for deal in deals:
        name = deal.get("name") or ""
        if _hard_bad_albert_name(name):
            rejected += 1
            if len(_ALBERT_STORAGE_STATS["samples"]) < 10:
                _ALBERT_STORAGE_STATS["samples"].append(legacy._clean(name)[:80])
            continue
        kept.append(deal)

    _ALBERT_STORAGE_STATS["kept"] = len(kept)
    _ALBERT_STORAGE_STATS["rejected"] = rejected
    return _BASE_REPLACE(store, kept, fingerprint)


def _sync_albert():
    old_replace = legacy._replace_store_deals
    legacy._replace_store_deals = _filtered_replace_store_deals
    try:
        count, errors = _OLD_ALBERT_SYNC()
    finally:
        legacy._replace_store_deals = old_replace

    samples = ", ".join(repr(x) for x in _ALBERT_STORAGE_STATS["samples"])
    _log(
        f"Albert storage filter: input={_ALBERT_STORAGE_STATS['input']}, "
        f"kept={_ALBERT_STORAGE_STATS['kept']}, rejected={_ALBERT_STORAGE_STATS['rejected']}"
        + (f"; samples={samples}" if samples else "")
    )
    # Discovery can report harmless dead viewer URLs. Once official rows were
    # successfully stored, they should not trigger the global fallback banner.
    return count, [] if count else errors


# The worker itself lives in v094 and resolves these functions dynamically.
v094._sync_lidl = _sync_lidl
v094._sync_albert = _sync_albert
v094._log = _log
v096._log = _log

start_worker = v096.start_worker
request_sync = v096.request_sync
