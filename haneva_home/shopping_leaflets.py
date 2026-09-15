from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import unescape
from urllib.parse import urljoin, urlparse
import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time
import unicodedata

import requests
from bs4 import BeautifulSoup

import shopping_official as core

SYNC_INTERVAL_SECONDS = 6 * 60 * 60
USER_AGENT = core.USER_AGENT

LIDL_HOME = "https://www.lidl.cz/"
LIDL_SEEDS = [
    "https://www.lidl.cz/c/ovoce-a-zelenina/a10080058",
]
LIDL_MAX_CAMPAIGNS = 14
LIDL_KEYWORDS = (
    "ovoce", "zelenina", "maso", "pecivo", "pečivo", "ryby", "nabidka", "nabídka",
    "sleva", "slevy", "kupon", "kupón", "potrav", "chlaz", "mlec", "mléč", "uzen",
    "vejce", "vikend", "víkend", "pondel", "ponděl", "ctvrtec", "čtvrtec", "cena",
)

ALBERT_DISCOVERY = [
    "https://www.albert.cz/aktualni-letaky",
    "https://letaky.albert.cz/",
]
ALBERT_MAX_TARGETS = 12
ALBERT_MAX_PDFS = 6
ALBERT_MAX_PDF_BYTES = 40 * 1024 * 1024

URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
ALBERT_URL_RE = re.compile(r"https?://letaky\.albert\.cz[^\s\"'<>\\]+", re.IGNORECASE)
PDF_URL_RE = re.compile(r"https?://letaky\.albert\.cz[^\s\"'<>\\]+?\.pdf(?:\?[^\s\"'<>\\]*)?", re.IGNORECASE)
LIDL_C_RE = re.compile(r"(?:https?://www\.lidl\.cz)?(/c/[a-z0-9áčďéěíňóřšťúůýž_\-]+/[as]\d+)", re.IGNORECASE)
PDF_PRICE_RE = re.compile(r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*Kč", re.IGNORECASE)
PDF_AMOUNT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|ks|bal\.?|balení)\b", re.IGNORECASE)

_LOCK = threading.Lock()
_THREAD = None
_STATE_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": None,
    "last_counts": {},
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value):
    value = _clean(value).lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value


def _headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,application/pdf,application/json;q=0.8,*/*;q=0.6",
    }


def _get_html(url, timeout=20):
    response = requests.get(url, headers=_headers(), timeout=timeout)
    response.raise_for_status()
    return response.text, response.url


def _decode_embedded_urls(text):
    text = unescape(str(text or ""))
    text = text.replace("\\/", "/")
    text = text.replace("\\u002F", "/").replace("\\u002f", "/")
    text = text.replace("\\u003A", ":").replace("\\u003a", ":")
    text = text.replace("%3F", "?").replace("%3f", "?")
    return text


def _date_range_near(text, start, end):
    left = max(0, start - 1800)
    right = min(len(text), end + 1800)
    return core._date_range_from_text(_clean(text[left:right]))


def _dedupe(deals):
    out = []
    seen = set()
    for deal in deals:
        name = _clean(deal.get("name"))
        price = deal.get("price_value")
        if not name or price is None:
            continue
        key = (
            core._norm(name),
            round(float(price), 2),
            deal.get("valid_from") or "",
            deal.get("valid_to") or "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(deal)
    return out


def _replace_store_deals(store, deals, fingerprint):
    """Atomically replace only the official rows for one retailer."""
    deals = _dedupe(deals)
    if not deals:
        return 0
    marker = f"leaflet-v093:{store}:{fingerprint}"
    now = _now_iso()
    with core.db() as conn:
        conn.execute("DELETE FROM official_deals WHERE store=? AND source_kind='official'", (store,))
        for deal in deals[:1800]:
            group_key, group_label = core._group_for(deal["name"])
            conn.execute("""
                INSERT INTO official_deals(
                    store, name, name_norm, group_key, group_label,
                    price_text, price_value, original_price, original_price_value,
                    discount_percent, amount, valid_from, valid_to,
                    source_url, source_kind, source_fingerprint, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                store,
                _clean(deal["name"])[:180],
                core._norm(deal["name"]),
                group_key,
                group_label,
                deal.get("price_text") or core._fmt_price(deal.get("price_value")),
                deal.get("price_value"),
                deal.get("original_price") or "",
                deal.get("original_price_value"),
                deal.get("discount_percent"),
                _clean(deal.get("amount"))[:100],
                deal.get("valid_from"),
                deal.get("valid_to"),
                _clean(deal.get("source_url"))[:1000],
                "official",
                marker,
                now,
            ))
        conn.commit()
    return len(deals)


def _source_status(store, source_url, fingerprint, status, error=None):
    try:
        core._source_status(store, source_url, fingerprint, status, error)
    except Exception:
        pass


# ---------- Lidl: official campaign pages ----------

def _lidl_campaign_candidates(home_html):
    soup = BeautifulSoup(home_html, "html.parser")
    candidates = []
    seen = set()

    def add(url, label=""):
        full = urljoin(LIDL_HOME, url)
        parsed = urlparse(full)
        if parsed.netloc not in {"www.lidl.cz", "lidl.cz"} or not parsed.path.startswith("/c/"):
            return
        if full in seen:
            return
        label_norm = _norm(label + " " + parsed.path)
        if not any(word in label_norm for word in LIDL_KEYWORDS):
            return
        seen.add(full)
        candidates.append(full)

    for seed in LIDL_SEEDS:
        if seed not in seen:
            seen.add(seed)
            candidates.append(seed)

    for a in soup.find_all("a", href=True):
        add(a.get("href"), a.get_text(" ", strip=True))

    decoded = _decode_embedded_urls(home_html)
    for match in LIDL_C_RE.finditer(decoded):
        # Some campaign links live inside JSON/script data. Use only a small
        # forward context so a neighbouring food campaign cannot accidentally
        # classify an unrelated non-food link.
        context = decoded[match.start(): min(len(decoded), match.end() + 180)]
        add(match.group(1), context)

    return candidates[:LIDL_MAX_CAMPAIGNS]


def _sync_lidl():
    home_html, final_url = _get_html(LIDL_HOME)
    campaigns = _lidl_campaign_candidates(home_html)
    all_deals = []
    fingerprints = [hashlib.sha256(home_html.encode("utf-8", errors="ignore")).hexdigest()]
    errors = []

    for url in campaigns:
        try:
            html, fetched_url = _get_html(url)
            fingerprints.append(hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest())
            parsed = core._parse_page("lidl", fetched_url, html)
            # Campaign pages may contain online-only non-food cross-sells. Keep rows that
            # have an in-store validity, or come from the always-food fruit/veg page.
            for deal in parsed:
                if deal.get("valid_from") or deal.get("valid_to") or "ovoce-a-zelenina" in fetched_url:
                    deal = dict(deal)
                    deal["source_url"] = deal.get("source_url") or fetched_url
                    all_deals.append(deal)
            _source_status("lidl", fetched_url, fingerprints[-1], "ok" if parsed else "parse-empty", None if parsed else "Bez rozpoznaných produktů")
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            _source_status("lidl", url, None, "error", str(exc)[:500])

    fingerprint = hashlib.sha256("|".join(fingerprints).encode()).hexdigest()
    count = _replace_store_deals("lidl", all_deals, fingerprint)
    if count == 0:
        raise RuntimeError("Lidl: z oficiálních akčních stránek se nepodařilo získat žádné produkty" + (f" ({errors[0]})" if errors else ""))
    return count, errors


# ---------- Albert: official leaflet PDFs ----------

def _albert_targets_from_html(html, base_url):
    decoded = _decode_embedded_urls(html)
    targets = []
    seen = set()

    def add(url, start=None, end=None):
        url = _decode_embedded_urls(url).rstrip(".,);]")
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("/"):
            url = urljoin(base_url, url)
        parsed = urlparse(url)
        if parsed.netloc != "letaky.albert.cz":
            return
        if url in seen:
            return
        seen.add(url)
        targets.append((url, start, end))

    for match in ALBERT_URL_RE.finditer(decoded):
        start, end = _date_range_near(decoded, match.start(), match.end())
        add(match.group(0), start, end)

    soup = BeautifulSoup(decoded, "html.parser")
    for tag in soup.find_all(["a", "iframe", "source"], href=True):
        add(tag.get("href"))
    for tag in soup.find_all(["iframe", "img", "script", "source"], src=True):
        add(tag.get("src"))

    return targets[:ALBERT_MAX_TARGETS]


def _discover_albert_pdfs():
    queue = []
    seen_pages = set()
    pdfs = []
    seen_pdfs = set()
    discovery_hashes = []
    errors = []

    for source in ALBERT_DISCOVERY:
        try:
            html, final_url = _get_html(source)
            discovery_hashes.append(hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest())
            queue.extend(_albert_targets_from_html(html, final_url))
            _source_status("albert", final_url, discovery_hashes[-1], "discovered", None)
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}: {exc}")
            _source_status("albert", source, None, "error", str(exc)[:500])

    while queue and len(pdfs) < ALBERT_MAX_PDFS:
        url, hinted_from, hinted_to = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        if ".pdf" in url.lower():
            if url not in seen_pdfs:
                seen_pdfs.add(url)
                pdfs.append((url, hinted_from, hinted_to))
            continue
        try:
            html, final_url = _get_html(url)
            discovery_hashes.append(hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest())
            own_range = core._date_range_from_text(_clean(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))[:30000])
            default_from = own_range[0] or hinted_from
            default_to = own_range[1] or hinted_to
            decoded = _decode_embedded_urls(html)
            for match in PDF_URL_RE.finditer(decoded):
                pdf_url = match.group(0).rstrip(".,);]")
                if pdf_url not in seen_pdfs:
                    seen_pdfs.add(pdf_url)
                    nearby = _date_range_near(decoded, match.start(), match.end())
                    pdfs.append((pdf_url, nearby[0] or default_from, nearby[1] or default_to))
                    if len(pdfs) >= ALBERT_MAX_PDFS:
                        break
            if len(pdfs) < ALBERT_MAX_PDFS:
                for child in _albert_targets_from_html(html, final_url):
                    if child[0] not in seen_pages:
                        queue.append((child[0], child[1] or default_from, child[2] or default_to))
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            _source_status("albert", url, None, "error", str(exc)[:500])

    fingerprint = hashlib.sha256("|".join(discovery_hashes + [p[0] for p in pdfs]).encode()).hexdigest()
    return pdfs[:ALBERT_MAX_PDFS], fingerprint, errors


def _download_pdf_to_temp(url):
    temp = tempfile.NamedTemporaryFile(prefix="haneva_albert_", suffix=".pdf", delete=False)
    path = temp.name
    temp.close()
    total = 0
    digest = hashlib.sha256()
    try:
        with requests.get(url, headers=_headers(), timeout=30, stream=True) as response:
            response.raise_for_status()
            ctype = (response.headers.get("content-type") or "").lower()
            if "pdf" not in ctype and ".pdf" not in response.url.lower():
                raise RuntimeError(f"neočekávaný typ obsahu {ctype or 'unknown'}")
            with open(path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > ALBERT_MAX_PDF_BYTES:
                        raise RuntimeError("PDF je větší než bezpečný limit 40 MB")
                    digest.update(chunk)
                    fh.write(chunk)
            final_url = response.url
        return path, digest.hexdigest(), final_url
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _pdf_to_text(path):
    proc = subprocess.run(
        ["pdftotext", "-layout", "-nopgbrk", path, "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=35,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("pdftotext: " + proc.stderr.decode("utf-8", errors="ignore")[:240])
    return proc.stdout.decode("utf-8", errors="ignore")


def _price_matches(text):
    cleaned = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|ks)\s*=\s*\d{1,4}(?:[.,]\d{1,2})?\s*Kč",
        " ", text, flags=re.IGNORECASE,
    )
    return [(m, core._float_price(m.group(0))) for m in PDF_PRICE_RE.finditer(cleaned)]


def _name_candidate(text):
    text = re.sub(r"[-–—]?\s*\d{1,3}\s*%", " ", text)
    text = re.sub(r"\b(super cena|akce|sleva|s aplikací|můj albert|club|kus)\b", " ", text, flags=re.IGNORECASE)
    text = _clean(text.strip(" |•·-–—:;"))
    if len(text) < 3 or not re.search(r"[A-Za-zÁ-ž]", text):
        return ""
    if len(text) > 150:
        text = text[-150:]
    return text


def _pdf_deals_from_text(text, source_url, hinted_from=None, hinted_to=None):
    if not text.strip():
        return []
    global_from, global_to = core._date_range_from_text(_clean(text[:25000]))
    valid_from = hinted_from or global_from
    valid_to = hinted_to or global_to
    deals = []
    carry_name = ""

    def emit(name, block_text, prices):
        name = _name_candidate(name)
        if not name or not prices:
            return
        values = [float(v) for v in prices if v is not None and 0 < float(v) < 100000]
        if not values:
            return
        current = min(values)
        original = max(values) if len(values) > 1 and max(values) > current + 0.01 else None
        local_from, local_to = core._date_range_from_text(block_text)
        amount_match = PDF_AMOUNT_RE.search(block_text)
        amount = amount_match.group(0) if amount_match else ""
        deals.append({
            "name": name,
            "price_value": current,
            "price_text": core._fmt_price(current),
            "original_price_value": original,
            "original_price": core._fmt_price(original),
            "discount_percent": core._discount_percent(current, original, block_text),
            "amount": amount,
            "valid_from": local_from or valid_from,
            "valid_to": local_to or valid_to,
            "source_url": source_url,
        })

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        columns = [c.strip() for c in re.split(r"\s{3,}", line) if c.strip()]
        if not columns:
            continue

        pending_name = carry_name
        pending_prices = []
        pending_text = []
        saw_price = False

        for column in columns:
            matches = [(m, v) for m, v in _price_matches(column) if v is not None and 0 < v < 100000]
            if matches:
                saw_price = True
                inline_name = _name_candidate(column[:matches[0][0].start()])
                if inline_name:
                    if pending_name and pending_prices:
                        emit(pending_name, " ".join(pending_text), pending_prices)
                    pending_name = inline_name
                    pending_prices = []
                    pending_text = []
                if pending_name:
                    pending_prices.extend(v for _, v in matches)
                    pending_text.append(column)
            else:
                candidate = _name_candidate(column)
                if candidate:
                    if pending_name and pending_prices:
                        emit(pending_name, " ".join(pending_text), pending_prices)
                        pending_prices = []
                        pending_text = []
                    pending_name = candidate

        if pending_name and pending_prices:
            emit(pending_name, " ".join(pending_text), pending_prices)
            carry_name = ""
        elif not saw_price and pending_name:
            carry_name = pending_name

    return _dedupe(deals)


def _sync_albert():
    pdfs, discovery_fingerprint, errors = _discover_albert_pdfs()
    if not pdfs:
        raise RuntimeError("Albert: na oficiálním webu se nepodařilo najít odkaz na leták/PDF" + (f" ({errors[0]})" if errors else ""))

    all_deals = []
    fingerprints = [discovery_fingerprint]
    for url, hinted_from, hinted_to in pdfs:
        path = None
        try:
            path, pdf_hash, final_url = _download_pdf_to_temp(url)
            fingerprints.append(pdf_hash)
            text = _pdf_to_text(path)
            parsed = _pdf_deals_from_text(text, final_url, hinted_from, hinted_to)
            all_deals.extend(parsed)
            _source_status("albert", final_url, pdf_hash, "ok" if parsed else "parse-empty", None if parsed else "PDF nemá použitelnou textovou vrstvu")
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            _source_status("albert", url, None, "error", str(exc)[:500])
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    fingerprint = hashlib.sha256("|".join(fingerprints).encode()).hexdigest()
    count = _replace_store_deals("albert", all_deals, fingerprint)
    if count == 0:
        raise RuntimeError("Albert: letáky byly nalezeny, ale z PDF se nepodařilo vytáhnout produkty" + (f" ({errors[0]})" if errors else ""))
    return count, errors


def _set_state(**values):
    with _STATE_LOCK:
        _STATE.update(values)
    # The existing API/UI reads shopping_official's sync state, keep it in sync.
    try:
        core._set_sync_state(**values)
    except Exception:
        pass


def _run_sync():
    if not _LOCK.acquire(blocking=False):
        return
    started = _now_iso()
    _set_state(running=True, last_started_at=started, last_error=None)
    counts = {"lidl": 0, "albert": 0}
    errors = []
    try:
        try:
            counts["lidl"], lidl_errors = _sync_lidl()
            errors.extend(lidl_errors)
        except Exception as exc:
            errors.append(f"lidl: {type(exc).__name__}: {exc}")
        try:
            counts["albert"], albert_errors = _sync_albert()
            errors.extend(albert_errors)
        except Exception as exc:
            errors.append(f"albert: {type(exc).__name__}: {exc}")
        try:
            core._cleanup()
        except Exception:
            pass
        _set_state(
            running=False,
            last_finished_at=_now_iso(),
            last_error="; ".join(errors)[:1600] if errors else None,
            last_counts=counts,
        )
    except Exception as exc:
        _set_state(running=False, last_finished_at=_now_iso(), last_error=f"{type(exc).__name__}: {exc}", last_counts=counts)
    finally:
        _LOCK.release()


def _worker():
    time.sleep(3)
    while True:
        _run_sync()
        time.sleep(SYNC_INTERVAL_SECONDS)


def start_worker():
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(target=_worker, name="haneva-leaflet-sync", daemon=True)
    _THREAD.start()


def request_sync():
    with _STATE_LOCK:
        if _STATE.get("running"):
            return dict(_STATE)
    threading.Thread(target=_run_sync, name="haneva-leaflet-sync-now", daemon=True).start()
    state = dict(_STATE)
    state["requested"] = True
    return state
