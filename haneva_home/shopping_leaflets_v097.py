from __future__ import annotations

import re

from bs4 import BeautifulSoup

import shopping_leaflets_v096 as v096

v095 = v096.v095
v094 = v096.v094
legacy = v096.legacy
core = v096.core

# 0.9.7 addresses two issues visible in real HA data:
# - Lidl product rows are now current, but structured data often contains only
#   the sale price even though the rendered product header also exposes an old
#   price / discount.
# - Albert pdftotext sometimes promotes reference quantities such as
#   "170–180 g", "1 dávka =" or "400–500 g" to the product-name column.

QUANTITY_ONLY_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?"
    r"(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:kg|g|l|ml|cl|ks|role|roli|d[aá]vk(?:a|y|u|ou))\b"
    r"(?:\s*(?:=|od|do|•|·|\*|x).*)?$",
    re.IGNORECASE,
)
QUANTITY_PREFIX_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?"
    r"(?:\s*[-–—]\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:kg|g|l|ml|cl|ks|role|roli|d[aá]vk(?:a|y|u|ou))\b",
    re.IGNORECASE,
)
REFERENCE_PRICE_CONTEXT_RE = re.compile(
    r"(?:100\s*g|1\s*kg|100\s*ml|1\s*l|za\s*(?:kg|l)|/\s*(?:kg|l))",
    re.IGNORECASE,
)
EXPLICIT_DISCOUNT_RE = re.compile(
    r"(?:sleva\s*)?(?:-|−)?\s*(?P<pct>\d{1,2})\s*%",
    re.IGNORECASE,
)


def _log(message):
    print(f"[Haneva][Akce 0.9.7] {message}", flush=True)


# ---------- Albert: keep quantities out of the name column ----------

_OLD_BAD_ALBERT_NAME = v095._bad_albert_name
_OLD_NAME_CANDIDATE = legacy._name_candidate


def _bad_albert_name(name):
    text = legacy._clean(name).strip(" |•·;:")
    if _OLD_BAD_ALBERT_NAME(text):
        return True
    if QUANTITY_ONLY_RE.match(text):
        return True

    # pdftotext occasionally appends a short marker after the quantity, e.g.
    # "170–180 g •" or "1 dávka od". If the whole candidate contains no useful
    # word besides a quantity/reference-price marker, it is not a product name.
    if QUANTITY_PREFIX_RE.match(text):
        remainder = QUANTITY_PREFIX_RE.sub("", text, count=1)
        remainder = re.sub(r"[=•·*/|()\[\],.;:+-]", " ", remainder)
        remainder = legacy._norm(remainder).strip()
        if remainder in {"", "od", "do", "od do", "cena", "za", "ks"}:
            return True

    norm = legacy._norm(text)
    if re.fullmatch(r"\d+\s*x\s*\d+(?:[.,]\d+)?\s*(?:g|kg|ml|l|ks)", norm):
        return True
    return False


def _name_candidate(text):
    candidate = _OLD_NAME_CANDIDATE(text)
    if not candidate or _bad_albert_name(candidate):
        return ""
    return candidate


# Patch both stages: filtering the finished row and, more importantly, candidate
# selection inside the PDF parser. Rejecting a quantity before it becomes the
# pending name lets the preceding real product name stay attached to its price.
legacy._name_candidate = _name_candidate
v095._bad_albert_name = _bad_albert_name


# ---------- Lidl: enrich current rows with old price / discount ----------

_OLD_LIDL_CHOOSE = v096._choose_lidl_product


def _explicit_discount(text):
    for match in EXPLICIT_DISCOUNT_RE.finditer(text or ""):
        try:
            pct = int(match.group("pct"))
        except Exception:
            continue
        if 1 <= pct <= 90:
            return pct
    return None


def _visible_old_price(product_text, current):
    """Find a plausible crossed/regular price without mistaking unit prices."""
    if current is None:
        return None
    candidates = []
    for match in core.PRICE_RE.finditer(product_text or ""):
        value = core._float_price(match.group(0))
        if value is None or value <= current + 0.01 or value > 100000:
            continue
        context = product_text[max(0, match.start() - 45): match.end() + 45]
        if REFERENCE_PRICE_CONTEXT_RE.search(context):
            continue
        candidates.append(value)
    # The closest price above the sale price is the safest old-price candidate;
    # larger values are often reference prices or unrelated badges.
    return min(candidates) if candidates else None


def _markup_old_price(html, current):
    if current is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    selectors = (
        "[class*='old-price']",
        "[class*='oldPrice']",
        "[class*='original-price']",
        "[class*='originalPrice']",
        "[class*='regular-price']",
        "[class*='regularPrice']",
        "[class*='strike']",
        "[class*='strikethrough']",
        "del",
        "s",
    )
    values = []
    for selector in selectors:
        try:
            nodes = soup.select(selector)
        except Exception:
            nodes = []
        for node in nodes[:20]:
            raw = node.get("content") or node.get_text(" ", strip=True)
            value = core._float_price(raw)
            if value is not None and current + 0.01 < value < 100000:
                values.append(value)
    return min(values) if values else None


def _json_old_price(html, current):
    if current is None:
        return None
    values = []
    # Lidl's frontend changes often; only explicit old/original/regular price
    # keys are trusted here. This deliberately ignores generic `price` fields.
    patterns = (
        r'"(?:oldPrice|originalPrice|regularPrice|listPrice|wasPrice)"\s*:\s*"?(?P<v>\d{1,5}(?:[.,]\d{1,2})?)',
        r'"(?:old_price|original_price|regular_price)"\s*:\s*"?(?P<v>\d{1,5}(?:[.,]\d{1,2})?)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html or "", re.IGNORECASE):
            value = core._float_price(match.group("v"))
            if value is not None and current + 0.01 < value < 100000:
                values.append(value)
    return min(values) if values else None


def _choose_lidl_product(url, html):
    deal = _OLD_LIDL_CHOOSE(url, html)
    if not deal:
        return deal

    deal = dict(deal)
    current = core._float_price(deal.get("price_value") or deal.get("price_text"))
    if current is None:
        return deal

    soup = BeautifulSoup(html, "html.parser")
    text = legacy._clean(soup.get_text(" ", strip=True))
    page_name = v096._page_name(soup)
    product_text = v096._main_product_block(text, page_name)

    original = core._float_price(deal.get("original_price_value") or deal.get("original_price"))
    if original is None or original <= current:
        original = _markup_old_price(html, current)
    if original is None:
        original = _visible_old_price(product_text, current)
    if original is None:
        original = _json_old_price(html, current)

    explicit_pct = _explicit_discount(product_text)
    if explicit_pct is None:
        # Search a little more broadly only for an explicitly written percent;
        # it is safe to show the retailer's percentage even when no old price is
        # available in a machine-readable form.
        explicit_pct = _explicit_discount(text[:30000])

    if original is not None and original > current:
        deal["original_price_value"] = original
        deal["original_price"] = core._fmt_price(original)
        deal["discount_percent"] = int(round((original - current) / original * 100))
    elif explicit_pct:
        deal["discount_percent"] = explicit_pct

    return deal


v096._choose_lidl_product = _choose_lidl_product


# Albert's 0.9.5 synchronizer already replaces all official Albert rows after a
# successful parse, so the bad quantity rows disappear on the next sync. Add a
# short 0.9.7 line to make this easy to verify from the add-on log.
_OLD_ALBERT_SYNC = v095._sync_albert


def _sync_albert():
    count, errors = _OLD_ALBERT_SYNC()
    _log(
        f"Albert cleaned: stored={count}, parsed={v095._ALBERT_STATS.get('parsed', 0)}, "
        f"kept={v095._ALBERT_STATS.get('kept', 0)}, rejected={v095._ALBERT_STATS.get('rejected', 0)}"
    )
    return count, errors


v094._sync_albert = _sync_albert
v094._log = _log

start_worker = v096.start_worker
request_sync = v096.request_sync
