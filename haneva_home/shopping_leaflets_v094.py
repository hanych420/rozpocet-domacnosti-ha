from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import unquote, urljoin, urlparse
import hashlib
import re
import time

from bs4 import BeautifulSoup

import shopping_leaflets as legacy

core = legacy.core

LIDL_CATEGORY_INDEX = "https://www.lidl.cz/c/kategorie/s10004543"
LIDL_WEEKLY_HUBS = [
    "https://www.lidl.cz/",
    "https://www.lidl.cz/c/posouvat-limity-to-se-vyplati/a10102510",
    LIDL_CATEGORY_INDEX,
]
LIDL_PRODUCT_LIMIT = 140
LIDL_PAGE_LIMIT = 24
FOOD_TERMS = (
    "potrav", "ovoce", "zelenina", "maso", "drube", "drůbe", "ryb", "syr",
    "mlec", "mléč", "vejce", "pekar", "pekár", "pecivo", "pečivo", "spiz",
    "spíž", "napoj", "nápoj", "kava", "káva", "caj", "čaj", "mraz", "uzen",
    "chlaz", "cereal", "sladk", "snack", "pondel", "ponděl", "ctvrtec",
    "čtvrtec", "vikend", "víkend", "znackove-slevy", "značkové slevy",
)
LIDL_PRODUCT_RE = re.compile(
    r'(?:https?://(?:www\.)?lidl\.cz)?(/p/[^"\'>\s\\]+?/p\d+)',
    re.IGNORECASE,
)
LIDL_CATEGORY_RE = re.compile(
    r'(?:https?://(?:www\.)?lidl\.cz)?(/c/[^"\'>\s\\]+?/[as]\d+)',
    re.IGNORECASE,
)

ALBERT_REL_PDF_RE = re.compile(
    r'(?:(?:https?:)?//letaky\.albert\.cz)?'
    r'(?P<path>/\d+/\d+/pdfs/[^"\'<>\s\\]+?\.pdf(?:\?[^"\'<>\s\\]*)?)',
    re.IGNORECASE,
)
ALBERT_VIEWER_RE = re.compile(
    r'(?:(?:https?:)?//letaky\.albert\.cz)?'
    r'(?P<path>/[a-z0-9][a-z0-9_-]+/(?:page/\d+(?:-\d+)?)?)',
    re.IGNORECASE,
)


def _log(message):
    print(f"[Haneva][Akce 0.9.4] {message}", flush=True)


def _decoded(text):
    value = legacy._decode_embedded_urls(text)
    try:
        value = unquote(value)
    except Exception:
        pass
    return value


def _is_foodish(text):
    n = legacy._norm(text)
    return any(term in n for term in FOOD_TERMS)


def _extract_lidl_links(html, base_url):
    decoded = _decoded(html)
    soup = BeautifulSoup(decoded, "html.parser")
    pages, products = [], []
    seen_pages, seen_products = set(), set()

    def add_page(raw, label=""):
        if not raw:
            return
        full = urljoin(base_url, raw)
        parsed = urlparse(full)
        if parsed.netloc not in {"www.lidl.cz", "lidl.cz"}:
            return
        if not parsed.path.startswith("/c/"):
            return
        if not _is_foodish(label + " " + parsed.path):
            return
        full = f"https://www.lidl.cz{parsed.path}"
        if full not in seen_pages:
            seen_pages.add(full)
            pages.append(full)

    def add_product(raw):
        if not raw:
            return
        full = urljoin(base_url, raw)
        parsed = urlparse(full)
        if parsed.netloc not in {"www.lidl.cz", "lidl.cz"}:
            return
        if "/p/" not in parsed.path:
            return
        full = f"https://www.lidl.cz{parsed.path}"
        if full not in seen_products:
            seen_products.add(full)
            products.append(full)

    for a in soup.find_all("a", href=True):
        href = a.get("href")
        label = a.get_text(" ", strip=True)
        add_page(href, label)
        add_product(href)

    for match in LIDL_CATEGORY_RE.finditer(decoded):
        context = decoded[max(0, match.start()-180):min(len(decoded), match.end()+220)]
        add_page(match.group(1), context)
    for match in LIDL_PRODUCT_RE.finditer(decoded):
        add_product(match.group(1))

    return pages, products


def _choose_lidl_product(url, html):
    soup = BeautifulSoup(html, "html.parser")
    text = legacy._clean(soup.get_text(" ", strip=True))
    valid_from, valid_to = core._date_range_from_text(text[:20000])
    if not (valid_from or valid_to):
        return None

    h1 = soup.find("h1")
    page_name = legacy._clean(h1.get_text(" ", strip=True) if h1 else "")
    parsed = core._parse_page("lidl", url, html)
    if page_name and parsed:
        target = core._norm(page_name)
        close = [
            d for d in parsed
            if target == core._norm(d.get("name"))
            or target in core._norm(d.get("name"))
            or core._norm(d.get("name")) in target
        ]
        if close:
            parsed = close

    if parsed:
        deal = dict(parsed[0])
        deal["valid_from"] = deal.get("valid_from") or valid_from
        deal["valid_to"] = deal.get("valid_to") or valid_to
        deal["source_url"] = url
        if page_name:
            deal["name"] = page_name
        return deal

    if not page_name:
        return None

    top = text.split("Číslo výrobku", 1)[0]
    prices = []
    for m in core.PRICE_RE.finditer(top):
        value = core._float_price(m.group(0))
        if value is not None and 0 < value < 100000:
            prices.append(value)
    if not prices:
        return None

    current = prices[0]
    if len(prices) > 1:
        current = prices[-1]
    original = max((p for p in prices if p > current + 0.01), default=None)
    amount_match = re.search(
        r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|ks|bal\.?|balení)\b",
        top, re.IGNORECASE
    )
    return {
        "name": page_name[:180],
        "price_value": current,
        "price_text": core._fmt_price(current),
        "original_price_value": original,
        "original_price": core._fmt_price(original),
        "discount_percent": core._discount_percent(current, original, top),
        "amount": amount_match.group(0) if amount_match else "",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "source_url": url,
    }


def _sync_lidl():
    pages = []
    products = []
    fingerprints = []
    errors = []
    seen_pages = set()
    seen_products = set()

    for url in LIDL_WEEKLY_HUBS:
        try:
            html, final_url = legacy._get_html(url)
            fingerprints.append(hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest())
            found_pages, found_products = _extract_lidl_links(html, final_url)
            for page in found_pages:
                if page not in seen_pages and len(pages) < LIDL_PAGE_LIMIT:
                    seen_pages.add(page)
                    pages.append(page)
            for product in found_products:
                if product not in seen_products and len(products) < LIDL_PRODUCT_LIMIT:
                    seen_products.add(product)
                    products.append(product)
            direct = core._parse_page("lidl", final_url, html)
            for deal in direct:
                if deal.get("valid_from") or deal.get("valid_to"):
                    products.append(("__deal__", deal))
            legacy._source_status("lidl", final_url, fingerprints[-1], "discovered", None)
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            legacy._source_status("lidl", url, None, "error", str(exc)[:500])

    for page in list(pages)[:LIDL_PAGE_LIMIT]:
        try:
            html, final_url = legacy._get_html(page)
            fingerprints.append(hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest())
            found_pages, found_products = _extract_lidl_links(html, final_url)
            for product in found_products:
                if product not in seen_products and len(seen_products) < LIDL_PRODUCT_LIMIT:
                    seen_products.add(product)
                    products.append(product)

            direct = core._parse_page("lidl", final_url, html)
            direct_count = 0
            for deal in direct:
                if deal.get("valid_from") or deal.get("valid_to"):
                    products.append(("__deal__", dict(deal)))
                    direct_count += 1
            legacy._source_status(
                "lidl", final_url, fingerprints[-1],
                "ok" if (direct_count or found_products) else "parse-empty",
                None if (direct_count or found_products) else "Bez produktových odkazů / rozpoznaných nabídek",
            )
        except Exception as exc:
            errors.append(f"{page}: {type(exc).__name__}: {exc}")
            legacy._source_status("lidl", page, None, "error", str(exc)[:500])

    deals = [entry[1] for entry in products if isinstance(entry, tuple) and entry and entry[0] == "__deal__"]
    product_urls = [entry for entry in products if isinstance(entry, str)]

    def fetch_product(url):
        try:
            html, final_url = legacy._get_html(url, timeout=16)
            fp = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()
            deal = _choose_lidl_product(final_url, html)
            legacy._source_status(
                "lidl", final_url, fp,
                "ok" if deal else "parse-empty",
                None if deal else "Produkt nemá rozpoznanou prodejnovou akci / platnost",
            )
            return deal, fp, None
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
    _log(f"Lidl: {len(pages)} food pages, {len(product_urls)} product URLs, {count} official deals")
    if count == 0:
        sample = errors[0] if errors else "bez dalších detailů"
        raise RuntimeError(f"Lidl: 0 oficiálních produktů; {sample}")
    return count, errors


def _pdf_candidates_from_html(html, base_url, default_from=None, default_to=None):
    decoded = _decoded(html)
    found = []
    seen = set()

    def add(raw, hinted_from=None, hinted_to=None):
        if not raw:
            return
        raw = unquote(legacy._decode_embedded_urls(raw)).replace("&amp;", "&")
        if raw.startswith("//"):
            raw = "https:" + raw
        full = urljoin(base_url, raw)
        parsed = urlparse(full)
        if parsed.netloc != "letaky.albert.cz" or ".pdf" not in parsed.path.lower():
            return
        if full in seen:
            return
        seen.add(full)
        found.append((full, hinted_from or default_from, hinted_to or default_to))

    for match in legacy.PDF_URL_RE.finditer(decoded):
        nearby = legacy._date_range_near(decoded, match.start(), match.end())
        add(match.group(0), nearby[0], nearby[1])
    for match in ALBERT_REL_PDF_RE.finditer(decoded):
        nearby = legacy._date_range_near(decoded, match.start(), match.end())
        add(match.group(0), nearby[0], nearby[1])

    soup = BeautifulSoup(decoded, "html.parser")
    for tag in soup.find_all(True):
        for value in tag.attrs.values():
            values = value if isinstance(value, list) else [value]
            for raw in values:
                raw = str(raw)
                if ".pdf" in raw.lower():
                    add(raw)
    return found


def _viewer_candidates(html, base_url):
    decoded = _decoded(html)
    found = []
    seen = set()
    final = urlparse(base_url)
    if final.netloc == "letaky.albert.cz" and final.path.strip("/"):
        root = f"https://letaky.albert.cz/{final.path.strip('/').split('/')[0]}/"
        seen.add(root)
        found.append(root)

    for match in ALBERT_VIEWER_RE.finditer(decoded):
        path = match.group("path")
        full = urljoin("https://letaky.albert.cz/", path)
        root = full.split("/page/", 1)[0].rstrip("/") + "/"
        if root not in seen:
            seen.add(root)
            found.append(root)
    return found[:12]


def _discover_albert_pdfs():
    queue = list(legacy.ALBERT_DISCOVERY)
    seen_pages = set()
    pdfs = []
    seen_pdfs = set()
    discovery_hashes = []
    errors = []
    viewers = []

    while queue and len(seen_pages) < 24 and len(pdfs) < legacy.ALBERT_MAX_PDFS:
        url = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            html, final_url = legacy._get_html(url)
            digest = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()
            discovery_hashes.append(digest)
            text = legacy._clean(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
            default_from, default_to = core._date_range_from_text(text[:30000])

            for pdf_url, hinted_from, hinted_to in _pdf_candidates_from_html(
                html, final_url, default_from, default_to
            ):
                if pdf_url not in seen_pdfs:
                    seen_pdfs.add(pdf_url)
                    pdfs.append((pdf_url, hinted_from, hinted_to))
                    if len(pdfs) >= legacy.ALBERT_MAX_PDFS:
                        break

            for viewer in _viewer_candidates(html, final_url):
                if viewer not in viewers:
                    viewers.append(viewer)
                if viewer not in seen_pages and viewer not in queue:
                    queue.append(viewer)

            for target, hinted_from, hinted_to in legacy._albert_targets_from_html(html, final_url):
                if ".pdf" in target.lower():
                    if target not in seen_pdfs:
                        seen_pdfs.add(target)
                        pdfs.append((target, hinted_from or default_from, hinted_to or default_to))
                elif target not in seen_pages and target not in queue:
                    queue.append(target)

            legacy._source_status("albert", final_url, digest, "discovered", None)
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            legacy._source_status("albert", url, None, "error", str(exc)[:500])

    if len(pdfs) < legacy.ALBERT_MAX_PDFS:
        for viewer in viewers[:8]:
            page1 = viewer.rstrip("/") + "/page/1"
            if page1 in seen_pages:
                continue
            try:
                html, final_url = legacy._get_html(page1)
                digest = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()
                discovery_hashes.append(digest)
                text = legacy._clean(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
                default_from, default_to = core._date_range_from_text(text[:30000])
                for candidate in _pdf_candidates_from_html(html, final_url, default_from, default_to):
                    if candidate[0] not in seen_pdfs:
                        seen_pdfs.add(candidate[0])
                        pdfs.append(candidate)
                        if len(pdfs) >= legacy.ALBERT_MAX_PDFS:
                            break
            except Exception as exc:
                errors.append(f"{page1}: {type(exc).__name__}: {exc}")

    fingerprint = hashlib.sha256(
        "|".join(discovery_hashes + [p[0] for p in pdfs]).encode()
    ).hexdigest()
    _log(f"Albert discovery: {len(seen_pages)} pages, {len(viewers)} viewers, {len(pdfs)} PDFs")
    return pdfs[:legacy.ALBERT_MAX_PDFS], fingerprint, errors


def _sync_albert():
    original = legacy._discover_albert_pdfs
    legacy._discover_albert_pdfs = _discover_albert_pdfs
    try:
        count, errors = legacy._sync_albert()
        _log(f"Albert: {count} official deals")
        return count, errors
    finally:
        legacy._discover_albert_pdfs = original


def _set_state(**values):
    legacy._set_state(**values)


def _run_sync():
    if not legacy._LOCK.acquire(blocking=False):
        return
    started = legacy._now_iso()
    _set_state(running=True, last_started_at=started, last_error=None)
    counts = {"lidl": 0, "albert": 0}
    errors = []
    _log("starting official leaflet sync")
    try:
        try:
            counts["lidl"], lidl_errors = _sync_lidl()
            errors.extend(lidl_errors)
        except Exception as exc:
            message = f"lidl: {type(exc).__name__}: {exc}"
            errors.append(message)
            _log(message)
        try:
            counts["albert"], albert_errors = _sync_albert()
            errors.extend(albert_errors)
        except Exception as exc:
            message = f"albert: {type(exc).__name__}: {exc}"
            errors.append(message)
            _log(message)
        try:
            core._cleanup()
        except Exception:
            pass
        _set_state(
            running=False,
            last_finished_at=legacy._now_iso(),
            last_error="; ".join(errors)[:2000] if errors else None,
            last_counts=counts,
        )
        _log(f"finished: Lidl={counts['lidl']}, Albert={counts['albert']}, errors={len(errors)}")
    except Exception as exc:
        _set_state(
            running=False,
            last_finished_at=legacy._now_iso(),
            last_error=f"{type(exc).__name__}: {exc}",
            last_counts=counts,
        )
    finally:
        legacy._LOCK.release()


def _worker():
    time.sleep(3)
    while True:
        _run_sync()
        time.sleep(legacy.SYNC_INTERVAL_SECONDS)


def start_worker():
    global _THREAD
    try:
        thread = _THREAD
    except NameError:
        thread = None
    if thread and thread.is_alive():
        return
    import threading
    _THREAD = threading.Thread(target=_worker, name="haneva-leaflet-sync-v094", daemon=True)
    _THREAD.start()


def request_sync():
    import threading
    with legacy._STATE_LOCK:
        if legacy._STATE.get("running"):
            return dict(legacy._STATE)
    threading.Thread(target=_run_sync, name="haneva-leaflet-sync-now-v094", daemon=True).start()
    state = dict(legacy._STATE)
    state["requested"] = True
    return state
