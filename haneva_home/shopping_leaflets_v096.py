from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
import hashlib
import re

from bs4 import BeautifulSoup

import shopping_leaflets_v095 as v095

v094 = v095.v094
legacy = v095.legacy
core = v095.core

# 0.9.5 proved that Lidl discovery reached real product pages, but it accepted
# dates found elsewhere on those pages (navigation/recommendations). That put
# stale rows into SQLite and the UI correctly filtered them out as non-current.
# 0.9.6 only trusts availability found in the main product block immediately
# before "Číslo výrobku" and prioritizes product links discovered from the
# weekly food campaign pages before broad category/index links.
LIDL_PRODUCT_LIMIT = 180
LIDL_PAGE_LIMIT = 30
LIDL_KEEP_PAST_DAYS = 7
LIDL_KEEP_FUTURE_DAYS = 21

_STATS = {
    "accepted": 0,
    "no_local_date": 0,
    "no_name": 0,
    "no_price": 0,
    "outside_window": 0,
}


def _log(message):
    print(f"[Haneva][Akce 0.9.6] {message}", flush=True)


def _page_name(soup):
    h1 = soup.find("h1")
    name = legacy._clean(h1.get_text(" ", strip=True) if h1 else "")
    if not name:
        meta = (
            soup.find("meta", attrs={"property": "og:title"})
            or soup.find("meta", attrs={"name": "twitter:title"})
        )
        name = legacy._clean(meta.get("content") if meta else "")
        name = re.sub(r"\s*\|\s*Lidl.*$", "", name, flags=re.IGNORECASE).strip()
    return name[:180]


def _main_product_block(text, page_name):
    """Return only the main Lidl product header, excluding recommendation rows."""
    article_end = text.find("Číslo výrobku")
    if article_end < 0:
        article_end = min(len(text), 18000)

    # The main product name is normally the last occurrence before the article
    # number. Earlier occurrences often belong to navigation/SEO data.
    start = -1
    if page_name:
        start = text.rfind(page_name, 0, article_end)
    if start < 0:
        start = max(0, article_end - 5000)
    return text[start:article_end]


def _local_validity(product_text):
    # Lidl currently uses forms such as:
    #   Pouze v prodejnách od 10.09. - 13.09.
    #   V prodejnách pouze 03.09. - 06.09.
    #   Také v prodejnách od 14.09. - 20.09.
    for match in v095.LIDL_STORE_RANGE_RE.finditer(product_text):
        context = product_text[max(0, match.start() - 160): match.end() + 100]
        if re.search(r"prodejn", context, re.IGNORECASE):
            return v095._range_from_match(match)
    return None, None


def _date_in_storage_window(valid_from, valid_to):
    today = date.today()
    try:
        start = date.fromisoformat(valid_from) if valid_from else None
    except Exception:
        start = None
    try:
        end = date.fromisoformat(valid_to) if valid_to else None
    except Exception:
        end = None
    if end and end < today - timedelta(days=LIDL_KEEP_PAST_DAYS):
        return False
    if start and start > today + timedelta(days=LIDL_KEEP_FUTURE_DAYS):
        return False
    return True


def _choose_lidl_product(url, html):
    soup = BeautifulSoup(html, "html.parser")
    text = legacy._clean(soup.get_text(" ", strip=True))
    page_name = _page_name(soup)
    if not page_name:
        _STATS["no_name"] += 1
        return None

    product_text = _main_product_block(text, page_name)
    valid_from, valid_to = _local_validity(product_text)
    if not (valid_from or valid_to):
        _STATS["no_local_date"] += 1
        return None
    if not _date_in_storage_window(valid_from, valid_to):
        _STATS["outside_window"] += 1
        return None

    # Prefer a structured offer only if it matches the actual main product.
    parsed = core._parse_page("lidl", url, html)
    target = core._norm(page_name)
    for candidate in parsed:
        name_norm = core._norm(candidate.get("name"))
        if not name_norm:
            continue
        if target == name_norm or target in name_norm or name_norm in target:
            deal = dict(candidate)
            deal["name"] = page_name
            # Never trust a date inherited from another card on the page.
            deal["valid_from"] = valid_from
            deal["valid_to"] = valid_to
            deal["source_url"] = url
            _STATS["accepted"] += 1
            return deal

    # Client-rendered Lidl product pages still expose the visible product header.
    prices = []
    for match in core.PRICE_RE.finditer(product_text):
        value = core._float_price(match.group(0))
        if value is not None and 0 < value < 100000:
            prices.append(value)
    if not prices:
        _STATS["no_price"] += 1
        return None

    current = prices[-1]
    original = max((p for p in prices[:-1] if p > current + 0.01), default=None)
    amount_match = re.search(
        r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|cl|ks|bal\.?|balení)\b",
        product_text,
        re.IGNORECASE,
    )
    _STATS["accepted"] += 1
    return {
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


def _append_unique(target, seen, values, limit):
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        target.append(value)
        if len(target) >= limit:
            break


def _sync_lidl():
    for key in _STATS:
        _STATS[key] = 0

    fingerprints = []
    errors = []
    campaign_pages = []
    campaign_seen = set()
    broad_products = []
    broad_seen = set()

    # Pass 1: homepage. We intentionally keep its direct product links only as
    # a fallback. Weekly food campaign pages get first chance at the product cap.
    try:
        html, final_url = legacy._get_html("https://www.lidl.cz/")
        fingerprints.append(hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest())
        pages, products = v094._extract_lidl_links(html, final_url)
        _append_unique(campaign_pages, campaign_seen, pages, LIDL_PAGE_LIMIT)
        _append_unique(broad_products, broad_seen, products, LIDL_PRODUCT_LIMIT)
    except Exception as exc:
        errors.append(f"homepage: {type(exc).__name__}: {exc}")

    # Pass 2: food category index contributes more campaign/category pages, but
    # its large generic product list must not consume the product limit first.
    try:
        html, final_url = legacy._get_html(v094.LIDL_CATEGORY_INDEX)
        fingerprints.append(hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest())
        pages, products = v094._extract_lidl_links(html, final_url)
        _append_unique(campaign_pages, campaign_seen, pages, LIDL_PAGE_LIMIT)
        _append_unique(broad_products, broad_seen, products, LIDL_PRODUCT_LIMIT)
    except Exception as exc:
        errors.append(f"category-index: {type(exc).__name__}: {exc}")

    priority_products = []
    priority_seen = set()
    direct_deals = []

    # Campaign/category pages are crawled BEFORE broad product URLs. This is the
    # important difference from 0.9.5, where the 140-product cap was already full
    # before current weekly campaign pages could contribute their products.
    for page in campaign_pages[:LIDL_PAGE_LIMIT]:
        try:
            html, final_url = legacy._get_html(page)
            fingerprints.append(hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest())
            _, products = v094._extract_lidl_links(html, final_url)
            _append_unique(priority_products, priority_seen, products, LIDL_PRODUCT_LIMIT)

            for deal in core._parse_page("lidl", final_url, html):
                vf, vt = deal.get("valid_from"), deal.get("valid_to")
                if (vf or vt) and _date_in_storage_window(vf, vt):
                    direct_deals.append(dict(deal))
        except Exception as exc:
            errors.append(f"{page}: {type(exc).__name__}: {exc}")

    product_urls = list(priority_products)
    product_seen = set(product_urls)
    if len(product_urls) < LIDL_PRODUCT_LIMIT:
        _append_unique(product_urls, product_seen, broad_products, LIDL_PRODUCT_LIMIT)

    deals = list(direct_deals)

    def fetch_product(url):
        try:
            html, final_url = legacy._get_html(url, timeout=16)
            fp = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()
            return _choose_lidl_product(final_url, html), fp, None
        except Exception as exc:
            return None, None, f"{url}: {type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch_product, url) for url in product_urls[:LIDL_PRODUCT_LIMIT]]
        for future in as_completed(futures):
            deal, fp, error = future.result()
            if fp:
                fingerprints.append(fp)
            if deal:
                deals.append(deal)
            if error:
                errors.append(error)

    fingerprint = hashlib.sha256("|".join(fingerprints).encode()).hexdigest()
    count = legacy._replace_store_deals("lidl", deals, fingerprint)

    today = date.today()
    current = upcoming = expired = 0
    for deal in legacy._dedupe(deals):
        try:
            start = date.fromisoformat(deal.get("valid_from")) if deal.get("valid_from") else None
        except Exception:
            start = None
        try:
            end = date.fromisoformat(deal.get("valid_to")) if deal.get("valid_to") else None
        except Exception:
            end = None
        if start and start > today:
            upcoming += 1
        elif end and end < today:
            expired += 1
        else:
            current += 1

    _log(
        f"Lidl: {len(campaign_pages)} campaign pages, {len(priority_products)} priority products, "
        f"{len(product_urls)} total product URLs, {count} stored; "
        f"current={current}, upcoming={upcoming}, expired={expired}"
    )
    _log(
        "Lidl parser: "
        f"accepted={_STATS['accepted']}, no_local_date={_STATS['no_local_date']}, "
        f"no_name={_STATS['no_name']}, no_price={_STATS['no_price']}, "
        f"outside_window={_STATS['outside_window']}"
    )
    if count == 0:
        sample = errors[0] if errors else "bez dalších detailů"
        raise RuntimeError(f"Lidl: 0 oficiálních produktů; {sample}")
    return count, errors


# Keep Albert's 0.9.5 cleanup unchanged. Replace only the Lidl synchronizer and
# product parser used by the existing worker.
v094._choose_lidl_product = _choose_lidl_product
v094._sync_lidl = _sync_lidl
v094._log = _log

start_worker = v094.start_worker
request_sync = v094.request_sync
