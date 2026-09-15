from __future__ import annotations

from datetime import date, timedelta
import re

from bs4 import BeautifulSoup

import shopping_leaflets_v094 as v094

legacy = v094.legacy
core = v094.core

# 0.9.4 diagnostics showed that Lidl discovery itself works (14 food pages,
# 140 product URLs), but every product was rejected before becoming a deal.
# Lidl product pages put the shop validity after a lot of navigation/content,
# so looking only at the first part of visible text was too strict.
LIDL_STORE_RANGE_RE = re.compile(
    r"(?:pouze\s+v\s+prodejn(?:a|á)ch\s*)?(?:od\s*)?"
    r"(?P<d1>\d{1,2})\.\s*(?P<m1>\d{1,2})\.\s*"
    r"(?:-|–|—|až|do)\s*"
    r"(?P<d2>\d{1,2})\.\s*(?P<m2>\d{1,2})\.",
    re.IGNORECASE,
)

UNIT_ONLY_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|cl|ks|role|bal(?:ení)?)\b",
    re.IGNORECASE,
)
ALBERT_HEADING_RE = re.compile(
    r"^\s*(?:bez\s+aplikace|s\s+aplikac[ií]|aplikace|m[uů]j\s+albert|"
    r"albert|akce|sleva|cena\s+za|platnost)\b",
    re.IGNORECASE,
)

_LIDL_STATS = {"accepted": 0, "no_date": 0, "no_name": 0, "no_price": 0}
_ALBERT_STATS = {"parsed": 0, "kept": 0, "rejected": 0}


def _log(message):
    print(f"[Haneva][Akce 0.9.5] {message}", flush=True)


def _range_from_match(match):
    today = date.today()
    try:
        d1 = int(match.group("d1"))
        m1 = int(match.group("m1"))
        d2 = int(match.group("d2"))
        m2 = int(match.group("m2"))
        y1 = today.year
        y2 = today.year
        start = date(y1, m1, d1)
        end = date(y2, m2, d2)
        if end < start:
            y2 += 1
            end = date(y2, m2, d2)
        # Around New Year, a December leaflet opened in January belongs to the
        # previous year; conversely an old spring range seen late in the year is
        # more likely content for the next cycle.
        if end < today - timedelta(days=180):
            y1 += 1
            y2 += 1
            start = date(y1, m1, d1)
            end = date(y2, m2, d2)
        elif start > today + timedelta(days=180):
            y1 -= 1
            y2 -= 1
            start = date(y1, m1, d1)
            end = date(y2, m2, d2)
        return start.isoformat(), end.isoformat()
    except Exception:
        return None, None


def _lidl_validity(html, visible_text):
    # Prefer the explicit in-store phrase. Searching the complete page is
    # intentional: on real Lidl pages it can sit well past the first 20 kB.
    combined = f"{visible_text} {v094._decoded(html)}"
    marker = re.search(r"pouze\s+v\s+prodejn(?:a|á)ch\s+od", combined, re.IGNORECASE)
    if marker:
        window = combined[marker.start(): marker.start() + 700]
        match = LIDL_STORE_RANGE_RE.search(window)
        if match:
            return _range_from_match(match)

    # Some campaigns omit the word 'Pouze' but still use an explicit 'od–do'
    # range next to the shop availability line.
    for match in LIDL_STORE_RANGE_RE.finditer(visible_text):
        context = visible_text[max(0, match.start() - 120): match.end() + 80]
        if re.search(r"prodejn|dostupn|v\s+prodeji", context, re.IGNORECASE):
            return _range_from_match(match)
    return None, None


def _choose_lidl_product(url, html):
    soup = BeautifulSoup(html, "html.parser")
    text = legacy._clean(soup.get_text(" ", strip=True))
    valid_from, valid_to = _lidl_validity(html, text)
    if not (valid_from or valid_to):
        _LIDL_STATS["no_date"] += 1
        return None

    h1 = soup.find("h1")
    page_name = legacy._clean(h1.get_text(" ", strip=True) if h1 else "")
    if not page_name:
        # Lidl also exposes product names in common metadata even if the visual
        # heading changes.
        meta = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "twitter:title"})
        page_name = legacy._clean(meta.get("content") if meta else "")
        page_name = re.sub(r"\s*\|\s*Lidl.*$", "", page_name, flags=re.IGNORECASE).strip()
    if not page_name:
        _LIDL_STATS["no_name"] += 1
        return None

    parsed = core._parse_page("lidl", url, html)
    if parsed:
        target = core._norm(page_name)
        close = [
            d for d in parsed
            if target == core._norm(d.get("name"))
            or target in core._norm(d.get("name"))
            or core._norm(d.get("name")) in target
        ]
        if close:
            deal = dict(close[0])
            deal["name"] = page_name[:180]
            deal["valid_from"] = deal.get("valid_from") or valid_from
            deal["valid_to"] = deal.get("valid_to") or valid_to
            deal["source_url"] = url
            _LIDL_STATS["accepted"] += 1
            return deal

    # Fallback for Lidl's client-rendered product pages. Limit the price scan to
    # the actual product header before 'Číslo výrobku' so recommendation prices
    # later on the page cannot become this product's price.
    start_idx = text.find(page_name)
    if start_idx < 0:
        start_idx = 0
    end_idx = text.find("Číslo výrobku", start_idx)
    if end_idx < 0:
        end_idx = min(len(text), start_idx + 7000)
    product_text = text[start_idx:end_idx]

    prices = []
    for match in core.PRICE_RE.finditer(product_text):
        value = core._float_price(match.group(0))
        if value is not None and 0 < value < 100000:
            prices.append(value)
    if not prices:
        _LIDL_STATS["no_price"] += 1
        return None

    # Lidl displays an old crossed-out price before the current one. With one
    # price it is naturally the current price; with several, the last displayed
    # price in the product header is the current promotional price.
    current = prices[-1]
    original = max((p for p in prices[:-1] if p > current + 0.01), default=None)
    amount_match = re.search(
        r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|cl|ks|bal\.?|balení)\b",
        product_text,
        re.IGNORECASE,
    )
    _LIDL_STATS["accepted"] += 1
    return {
        "name": page_name[:180],
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


def _sync_lidl():
    for key in _LIDL_STATS:
        _LIDL_STATS[key] = 0

    # The hard-coded campaign URL used in 0.9.4 is already returning 404. Home
    # and the category index are sufficient discovery roots and do not depend on
    # a short-lived campaign slug.
    old_hubs = v094.LIDL_WEEKLY_HUBS
    v094.LIDL_WEEKLY_HUBS = ["https://www.lidl.cz/", v094.LIDL_CATEGORY_INDEX]
    try:
        return v094._sync_lidl_original()
    finally:
        v094.LIDL_WEEKLY_HUBS = old_hubs
        _log(
            "Lidl parser: "
            f"accepted={_LIDL_STATS['accepted']}, no_date={_LIDL_STATS['no_date']}, "
            f"no_name={_LIDL_STATS['no_name']}, no_price={_LIDL_STATS['no_price']}"
        )


def _bad_albert_name(name):
    text = legacy._clean(name)
    norm = legacy._norm(text)
    if not text:
        return True
    if ALBERT_HEADING_RE.search(text):
        return True
    if UNIT_ONLY_RE.search(text):
        return True
    # Typical pdftotext artefacts from the reference-price column: '100 g =',
    # '1 kg •', '100 g od', etc.
    if re.match(r"^\s*\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|cl|ks|role)\b", text, re.IGNORECASE):
        return True
    if norm in {"od", "do", "od do", "cena", "super cena", "club", "muj albert"}:
        return True
    letters = re.findall(r"[A-Za-zÁ-ž]", text)
    if len(letters) < 3:
        return True
    return False


_ORIGINAL_PDF_PARSER = legacy._pdf_deals_from_text


def _pdf_deals_clean(text, source_url, hinted_from=None, hinted_to=None):
    deals = _ORIGINAL_PDF_PARSER(text, source_url, hinted_from, hinted_to)
    _ALBERT_STATS["parsed"] += len(deals)
    kept = []
    for deal in deals:
        if _bad_albert_name(deal.get("name")):
            _ALBERT_STATS["rejected"] += 1
            continue
        kept.append(deal)
    _ALBERT_STATS["kept"] += len(kept)
    return kept


def _sync_albert():
    for key in _ALBERT_STATS:
        _ALBERT_STATS[key] = 0
    original_parser = legacy._pdf_deals_from_text
    legacy._pdf_deals_from_text = _pdf_deals_clean
    try:
        return v094._sync_albert_original()
    finally:
        legacy._pdf_deals_from_text = original_parser
        _log(
            "Albert parser: "
            f"parsed={_ALBERT_STATS['parsed']}, kept={_ALBERT_STATS['kept']}, "
            f"rejected={_ALBERT_STATS['rejected']}"
        )


# Keep references to 0.9.4 implementations before replacing their globals. The
# existing worker/run loop resolves these globals at runtime, so no second copy
# of the synchronization framework is needed.
v094._sync_lidl_original = v094._sync_lidl
v094._sync_albert_original = v094._sync_albert
v094._choose_lidl_product = _choose_lidl_product
v094._sync_lidl = _sync_lidl
v094._sync_albert = _sync_albert
v094._log = _log

start_worker = v094.start_worker
request_sync = v094.request_sync
