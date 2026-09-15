from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata

import requests
from bs4 import BeautifulSoup

DB_PATH = "/data/shopping.db"
SYNC_INTERVAL_SECONDS = 6 * 60 * 60
RETENTION_DAYS = 90
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)
SOURCES = {
    "lidl": [
        "https://www.lidl.cz/c/akcni-nabidka/s10008933",
        "https://www.lidl.cz/",
    ],
    "albert": [
        "https://www.albert.cz/aktualni-letaky",
    ],
}

_SYNC_LOCK = threading.Lock()
_SYNC_STATE_LOCK = threading.Lock()
_SYNC_THREAD = None
_SYNC_STATE = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": None,
    "last_counts": {},
}

PRICE_RE = re.compile(
    r"(?<!\d)(\d{1,4}(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*(?:Kč|CZK)",
    re.IGNORECASE,
)
DATE_RANGE_RE = re.compile(
    r"(?P<d1>\d{1,2})\.\s*(?P<m1>\d{1,2})\.(?:\s*(?P<y1>\d{4}))?"
    r"\s*(?:-|–|—|až|do)\s*"
    r"(?P<d2>\d{1,2})\.\s*(?P<m2>\d{1,2})\.(?:\s*(?P<y2>\d{4}))?",
    re.IGNORECASE,
)
DATE_SINGLE_RE = re.compile(
    r"(?P<d>\d{1,2})\.\s*(?P<m>\d{1,2})\.(?:\s*(?P<y>\d{4}))?"
)
PERCENT_RE = re.compile(r"(-?\d{1,3}(?:[.,]\d+)?)\s*%")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value):
    value = _clean(value).lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value)[:240]


def _float_price(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    text = _clean(value)
    match = PRICE_RE.search(text)
    raw = match.group(1) if match else re.sub(r"[^0-9,.\s\u00a0]", "", text)
    raw = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except Exception:
        return None


def _fmt_price(value):
    if value is None:
        return ""
    if abs(value - round(value)) < 0.005:
        return f"{int(round(value))} Kč"
    return f"{value:.2f}".replace(".", ",") + " Kč"


def _iso_date(value):
    if not value:
        return None
    text = _clean(value)
    for candidate in (text[:10], text):
        try:
            return date.fromisoformat(candidate).isoformat()
        except Exception:
            pass
    return None


def _date_range_from_text(text, today=None):
    today = today or date.today()
    text = _clean(text)
    match = DATE_RANGE_RE.search(text)
    if match:
        d1, m1 = int(match.group("d1")), int(match.group("m1"))
        d2, m2 = int(match.group("d2")), int(match.group("m2"))
        y1 = int(match.group("y1")) if match.group("y1") else None
        y2 = int(match.group("y2")) if match.group("y2") else None

        if y2 and not y1:
            y1 = y2
        if y1 and not y2:
            y2 = y1
        if not y1 and not y2:
            y1 = today.year
            y2 = today.year
            try:
                probe_end = date(y2, m2, d2)
                probe_start = date(y1, m1, d1)
                if probe_end < probe_start:
                    y2 += 1
                elif probe_end < today - timedelta(days=180):
                    y1 += 1
                    y2 += 1
            except Exception:
                pass
        try:
            return date(y1, m1, d1).isoformat(), date(y2, m2, d2).isoformat()
        except Exception:
            return None, None
    return None, None


def _discount_percent(current, original, text=""):
    if current is not None and original is not None and original > current:
        return int(round((original - current) / original * 100))
    match = PERCENT_RE.search(_clean(text))
    if match:
        try:
            return int(round(abs(float(match.group(1).replace(",", ".")))))
        except Exception:
            pass
    return None


def _group_for(name):
    n = _norm(name)

    rules = [
        (("rajcat", "plech"), ("rajcata-v-konzerve", "Rajčata v konzervě")),
        (("rajcat", "konzerv"), ("rajcata-v-konzerve", "Rajčata v konzervě")),
        (("rajcat", "drcen"), ("rajcata-v-konzerve", "Rajčata v konzervě")),
        (("rajcat", "loupan"), ("rajcata-v-konzerve", "Rajčata v konzervě")),
        (("kurec", "prs"), ("kureci-prsa", "Kuřecí prsa")),
        (("kurec", "palick"), ("kureci-palicky", "Kuřecí paličky")),
        (("kurec", "steh"), ("kureci-stehna", "Kuřecí stehna")),
        (("kurec", "kridl"), ("kureci-kridla", "Kuřecí křídla")),
        (("veprov", "krkov"), ("veprova-krkovice", "Vepřová krkovice")),
        (("veprov", "kyta"), ("veprova-kyta", "Vepřová kýta")),
        (("veprov", "panenk"), ("veprova-panenka", "Vepřová panenka")),
        (("veprov", "plec"), ("veprova-plec", "Vepřová plec")),
        (("veprov", "bok"), ("veprovy-bok", "Vepřový bok")),
        (("hovez",), ("hovezi-maso", "Hovězí maso")),
        (("chleb",), ("chleb", "Chléb")),
        (("rohlik",), ("pecivo", "Pečivo")),
        (("housk",), ("pecivo", "Pečivo")),
        (("baget",), ("pecivo", "Pečivo")),
        (("rajcat",), ("rajcata", "Rajčata")),
        (("paprik",), ("zelenina", "Zelenina")),
        (("okurk",), ("zelenina", "Zelenina")),
        (("salat",), ("zelenina", "Zelenina")),
        (("cibul",), ("zelenina", "Zelenina")),
        (("mrkev",), ("zelenina", "Zelenina")),
        (("brambor",), ("zelenina", "Zelenina")),
        (("cuketa",), ("zelenina", "Zelenina")),
        (("avokad",), ("zelenina", "Zelenina")),
        (("banan",), ("ovoce", "Ovoce")),
        (("jabl",), ("ovoce", "Ovoce")),
        (("pomeranc",), ("ovoce", "Ovoce")),
        (("mandar",), ("ovoce", "Ovoce")),
        (("citron",), ("ovoce", "Ovoce")),
        (("mlek",), ("mleko", "Mléko a nápoje")),
        (("jogurt",), ("jogurty", "Jogurty")),
        (("syr",), ("syry", "Sýry")),
        (("eidam",), ("syry", "Sýry")),
        (("gouda",), ("syry", "Sýry")),
        (("vejce",), ("vejce", "Vejce")),
        (("testovin",), ("testoviny", "Těstoviny")),
        (("ryze",), ("ryze", "Rýže")),
        (("tunak",), ("konzervy", "Konzervy")),
        (("sunka",), ("uzeniny", "Šunky a uzeniny")),
        (("slanina",), ("uzeniny", "Šunky a uzeniny")),
    ]
    for needles, result in rules:
        if all(needle in n for needle in needles):
            return result

    if "kurec" in n:
        return "kureci-maso", "Kuřecí maso"
    if "veprov" in n:
        return "veprove-maso", "Vepřové maso"
    if any(word in n for word in ("zelenin", "brokol", "kvetak", "celer", "porek")):
        return "zelenina", "Zelenina"
    if any(word in n for word in ("ovoce", "hroz", "jahod", "boruv", "malin")):
        return "ovoce", "Ovoce"

    words = [w.capitalize() for w in _clean(name).split()[:3]]
    label = " ".join(words)[:80] or "Ostatní"
    return "ostatni-" + hashlib.sha1(_norm(label).encode()).hexdigest()[:8], label


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS official_sources (
                store TEXT NOT NULL,
                source_url TEXT NOT NULL,
                fingerprint TEXT,
                fetched_at TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                last_error TEXT,
                PRIMARY KEY(store, source_url)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS official_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store TEXT NOT NULL,
                name TEXT NOT NULL,
                name_norm TEXT NOT NULL,
                group_key TEXT NOT NULL,
                group_label TEXT NOT NULL,
                price_text TEXT NOT NULL,
                price_value REAL,
                original_price TEXT NOT NULL DEFAULT '',
                original_price_value REAL,
                discount_percent INTEGER,
                amount TEXT NOT NULL DEFAULT '',
                valid_from TEXT,
                valid_to TEXT,
                source_url TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'official',
                source_fingerprint TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_official_deals_filter
            ON official_deals(store, valid_from, valid_to, group_key)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shopping_item_deals (
                item_id INTEGER PRIMARY KEY,
                store TEXT NOT NULL DEFAULT 'any',
                deal_id INTEGER,
                price TEXT NOT NULL DEFAULT '',
                original_price TEXT NOT NULL DEFAULT '',
                valid_from TEXT,
                valid_to TEXT,
                source_url TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT '',
                attached_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shopping_sync_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()


def _source_status(store, url, fingerprint=None, status="ok", error=None):
    with db() as conn:
        conn.execute("""
            INSERT INTO official_sources(store, source_url, fingerprint, fetched_at, status, last_error)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(store, source_url) DO UPDATE SET
                fingerprint=COALESCE(excluded.fingerprint, official_sources.fingerprint),
                fetched_at=excluded.fetched_at,
                status=excluded.status,
                last_error=excluded.last_error
        """, (store, url, fingerprint, _now_iso(), status, error))
        conn.commit()


def _old_fingerprint(store, url):
    with db() as conn:
        row = conn.execute(
            "SELECT fingerprint FROM official_sources WHERE store=? AND source_url=?",
            (store, url),
        ).fetchone()
    return row["fingerprint"] if row else None


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _offer_from_object(obj):
    offers = obj.get("offers") or obj.get("offer")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        offers = obj

    name = _clean(obj.get("name") or obj.get("title") or obj.get("productName") or "")
    price_raw = (
        offers.get("price")
        or offers.get("lowPrice")
        or offers.get("salePrice")
        or offers.get("currentPrice")
        or obj.get("salePrice")
        or obj.get("currentPrice")
        or obj.get("price")
    )
    current = _float_price(price_raw)
    if not name or len(name) < 2 or current is None or current <= 0 or current > 100000:
        return None

    old_raw = (
        offers.get("highPrice")
        or offers.get("oldPrice")
        or offers.get("originalPrice")
        or offers.get("regularPrice")
        or obj.get("oldPrice")
        or obj.get("originalPrice")
        or obj.get("regularPrice")
    )
    original = _float_price(old_raw)
    if original is not None and original <= current:
        original = None

    valid_from = _iso_date(
        offers.get("validFrom")
        or offers.get("availabilityStarts")
        or obj.get("validFrom")
        or obj.get("startDate")
    )
    valid_to = _iso_date(
        offers.get("priceValidUntil")
        or offers.get("validThrough")
        or offers.get("availabilityEnds")
        or obj.get("validTo")
        or obj.get("endDate")
    )
    amount = _clean(
        obj.get("size")
        or obj.get("amount")
        or obj.get("unit")
        or obj.get("weight")
        or obj.get("description")
        or ""
    )[:100]
    url = _clean(obj.get("url") or offers.get("url") or "")
    text = json.dumps(obj, ensure_ascii=False)[:6000]
    return {
        "name": name[:180],
        "price_value": current,
        "price_text": _fmt_price(current),
        "original_price_value": original,
        "original_price": _fmt_price(original),
        "discount_percent": _discount_percent(current, original, text),
        "amount": amount,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "item_url": url,
    }


def _page_default_range(soup):
    text = _clean(soup.get_text(" ", strip=True))
    return _date_range_from_text(text[:25000])


def _parse_json_scripts(soup):
    deals = []
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        text = script.string or script.get_text("", strip=False) or ""
        if not text or len(text) > 6_000_000:
            continue
        payload = None
        if "json" in script_type:
            try:
                payload = json.loads(text)
            except Exception:
                payload = None
        elif text.lstrip().startswith(("{", "[")) and any(
            needle in text for needle in ('"price"', '"salePrice"', '"currentPrice"')
        ):
            try:
                payload = json.loads(text)
            except Exception:
                payload = None
        if payload is None:
            continue
        for obj in _walk_json(payload):
            item = _offer_from_object(obj)
            if item:
                deals.append(item)
    return deals


def _parse_html_cards(soup):
    deals = []
    card_selectors = [
        "[data-testid*='product']",
        "[class*='product-card']",
        "[class*='product_card']",
        "[class*='product-tile']",
        "[class*='offer-tile']",
        "article[class*='product']",
    ]
    cards = []
    seen_ids = set()
    for selector in card_selectors:
        try:
            found = soup.select(selector)
        except Exception:
            found = []
        for card in found:
            ident = id(card)
            if ident not in seen_ids:
                seen_ids.add(ident)
                cards.append(card)
        if len(cards) > 500:
            break

    name_selectors = [
        "[itemprop='name']",
        "[class*='product-name']",
        "[class*='product_name']",
        "[class*='product-title']",
        "[class*='product_title']",
        "h2", "h3",
    ]
    price_selectors = [
        "[itemprop='price']",
        "[class*='sale-price']",
        "[class*='current-price']",
        "[class*='price']",
    ]

    for card in cards[:500]:
        name = ""
        for selector in name_selectors:
            el = card.select_one(selector)
            if el:
                name = _clean(el.get("content") or el.get_text(" ", strip=True))
                if 2 <= len(name) <= 180:
                    break
                name = ""
        if not name:
            continue

        current = None
        price_text = ""
        for selector in price_selectors:
            for el in card.select(selector)[:8]:
                raw = el.get("content") or el.get_text(" ", strip=True)
                value = _float_price(raw)
                if value is not None and 0 < value < 100000:
                    current = value
                    price_text = _fmt_price(value)
                    break
            if current is not None:
                break
        if current is None:
            continue

        card_text = _clean(card.get_text(" ", strip=True))
        prices = []
        for match in PRICE_RE.finditer(card_text):
            value = _float_price(match.group(0))
            if value is not None and value > 0:
                prices.append(value)
        original = min((p for p in prices if p > current + 0.01), default=None)

        valid_from, valid_to = _date_range_from_text(card_text)
        href = ""
        link = card.find("a", href=True)
        if link:
            href = link.get("href", "")

        amount = ""
        amount_match = re.search(
            r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|ks|bal\.?)\b",
            card_text,
            re.IGNORECASE,
        )
        if amount_match:
            amount = amount_match.group(0)

        deals.append({
            "name": name,
            "price_value": current,
            "price_text": price_text,
            "original_price_value": original,
            "original_price": _fmt_price(original),
            "discount_percent": _discount_percent(current, original, card_text),
            "amount": amount,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "item_url": href,
        })
    return deals


def _dedupe_deals(deals, base_url, page_range):
    result = []
    seen = set()
    page_from, page_to = page_range
    for deal in deals:
        name = _clean(deal.get("name"))
        current = deal.get("price_value")
        if not name or current is None:
            continue
        valid_from = deal.get("valid_from") or page_from
        valid_to = deal.get("valid_to") or page_to
        key = (_norm(name), round(float(current), 2), valid_from or "", valid_to or "")
        if key in seen:
            continue
        seen.add(key)
        item_url = deal.get("item_url") or base_url
        if item_url and not item_url.startswith(("http://", "https://")):
            item_url = urljoin(base_url, item_url)
        deal = dict(deal)
        deal["source_url"] = item_url or base_url
        deal["valid_from"] = valid_from
        deal["valid_to"] = valid_to
        result.append(deal)
    return result


def _parse_page(store, url, html):
    soup = BeautifulSoup(html, "html.parser")
    page_range = _page_default_range(soup)
    deals = _parse_json_scripts(soup)
    deals.extend(_parse_html_cards(soup))
    deals = _dedupe_deals(deals, url, page_range)
    return [d for d in deals if 2 <= len(d["name"]) <= 180 and d["price_value"] > 0][:1200]


def _replace_source_deals(store, source_url, fingerprint, deals):
    now = _now_iso()
    with db() as conn:
        conn.execute(
            "DELETE FROM official_deals WHERE store=? AND source_url=?",
            (store, source_url),
        )
        for deal in deals:
            group_key, group_label = _group_for(deal["name"])
            conn.execute("""
                INSERT INTO official_deals(
                    store, name, name_norm, group_key, group_label,
                    price_text, price_value, original_price, original_price_value,
                    discount_percent, amount, valid_from, valid_to,
                    source_url, source_kind, source_fingerprint, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                store,
                deal["name"][:180],
                _norm(deal["name"]),
                group_key,
                group_label,
                deal.get("price_text") or _fmt_price(deal.get("price_value")),
                deal.get("price_value"),
                deal.get("original_price") or "",
                deal.get("original_price_value"),
                deal.get("discount_percent"),
                _clean(deal.get("amount"))[:100],
                deal.get("valid_from"),
                deal.get("valid_to"),
                source_url,
                "official",
                fingerprint,
                now,
            ))
        conn.commit()


def _cleanup():
    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    with db() as conn:
        conn.execute(
            "DELETE FROM official_deals WHERE valid_to IS NOT NULL AND valid_to < ?",
            (cutoff,),
        )
        conn.commit()


def _sync_once():
    counts = {}
    errors = []
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }

    for store, urls in SOURCES.items():
        store_count = 0
        for url in urls:
            try:
                response = requests.get(url, headers=headers, timeout=20)
                response.raise_for_status()
                html = response.text
                fingerprint = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()
                old = _old_fingerprint(store, url)
                if old == fingerprint:
                    with db() as conn:
                        row = conn.execute(
                            "SELECT COUNT(*) AS n FROM official_deals WHERE store=? AND source_url=?",
                            (store, url),
                        ).fetchone()
                    n = int(row["n"] if row else 0)
                    store_count += n
                    _source_status(store, url, fingerprint, "unchanged", None)
                    continue

                parsed = _parse_page(store, url, html)
                if parsed:
                    _replace_source_deals(store, url, fingerprint, parsed)
                    store_count += len(parsed)
                    _source_status(store, url, fingerprint, "ok", None)
                else:
                    _source_status(
                        store,
                        url,
                        fingerprint,
                        "parse-empty",
                        "Na stránce se nepodařilo bezpečně rozpoznat žádné nabídky.",
                    )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                errors.append(f"{store}: {message}")
                _source_status(store, url, None, "error", message)
        counts[store] = store_count

    _cleanup()
    return counts, errors


def _set_sync_state(**values):
    with _SYNC_STATE_LOCK:
        _SYNC_STATE.update(values)


def _run_sync():
    if not _SYNC_LOCK.acquire(blocking=False):
        return
    try:
        _set_sync_state(running=True, last_started_at=_now_iso(), last_error=None)
        counts, errors = _sync_once()
        _set_sync_state(
            running=False,
            last_finished_at=_now_iso(),
            last_error="; ".join(errors) if errors else None,
            last_counts=counts,
        )
    except Exception as exc:
        _set_sync_state(
            running=False,
            last_finished_at=_now_iso(),
            last_error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        _SYNC_LOCK.release()


def _worker():
    time.sleep(3)
    while True:
        _run_sync()
        time.sleep(SYNC_INTERVAL_SECONDS)


def start_worker():
    global _SYNC_THREAD
    if _SYNC_THREAD and _SYNC_THREAD.is_alive():
        return
    _SYNC_THREAD = threading.Thread(
        target=_worker,
        name="haneva-shopping-official",
        daemon=True,
    )
    _SYNC_THREAD.start()


def request_sync():
    with _SYNC_STATE_LOCK:
        if _SYNC_STATE["running"]:
            return dict(_SYNC_STATE)
    threading.Thread(target=_run_sync, name="haneva-shopping-sync-now", daemon=True).start()
    state = dict(_SYNC_STATE)
    state["requested"] = True
    return state


def _timing(valid_from, valid_to):
    today = date.today()
    start = None
    end = None
    try:
        start = date.fromisoformat(valid_from) if valid_from else None
    except Exception:
        pass
    try:
        end = date.fromisoformat(valid_to) if valid_to else None
    except Exception:
        pass

    if start and today < start:
        days = (start - today).days
        if days == 1:
            message = "Akce začíná zítra"
        else:
            message = f"Akce od {start.day}. {start.month}."
        return "upcoming", message

    if end and today > end:
        return "expired", f"Akce skončila {end.day}. {end.month}."

    if end:
        days = (end - today).days
        if days == 0:
            return "ending", "Akce končí dnes"
        if days == 1:
            return "ending", "Akce končí zítra"
        if days == 2:
            return "ending", "Akce končí pozítří"

    return "active", ""


def attach_item(item_id, payload):
    if not item_id:
        return
    store = _clean(payload.get("preferred_store") or payload.get("store") or "any").lower()
    if store not in {"lidl", "albert", "any"}:
        store = "any"
    with db() as conn:
        conn.execute("""
            INSERT INTO shopping_item_deals(
                item_id, store, deal_id, price, original_price,
                valid_from, valid_to, source_url, source_kind, attached_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                store=excluded.store,
                deal_id=excluded.deal_id,
                price=excluded.price,
                original_price=excluded.original_price,
                valid_from=excluded.valid_from,
                valid_to=excluded.valid_to,
                source_url=excluded.source_url,
                source_kind=excluded.source_kind,
                attached_at=excluded.attached_at
        """, (
            int(item_id),
            store,
            payload.get("deal_id"),
            _clean(payload.get("price"))[:80],
            _clean(payload.get("original_price"))[:80],
            payload.get("valid_from") or None,
            payload.get("valid_to") or None,
            _clean(payload.get("source_url"))[:1000],
            _clean(payload.get("source_kind"))[:80],
            _now_iso(),
        ))
        conn.commit()


def enrich_state(state):
    state = dict(state or {})
    items = [dict(item) for item in state.get("items", [])]
    ids = [int(item["id"]) for item in items if item.get("id") is not None]
    attached = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        with db() as conn:
            rows = conn.execute(
                f"SELECT * FROM shopping_item_deals WHERE item_id IN ({placeholders})",
                ids,
            ).fetchall()
        attached = {int(row["item_id"]): dict(row) for row in rows}

    for item in items:
        meta = attached.get(int(item.get("id") or 0))
        if not meta:
            continue
        status, message = _timing(meta.get("valid_from"), meta.get("valid_to"))
        item["deal"] = {
            "store": meta.get("store"),
            "price": meta.get("price"),
            "original_price": meta.get("original_price"),
            "valid_from": meta.get("valid_from"),
            "valid_to": meta.get("valid_to"),
            "source_url": meta.get("source_url"),
            "source_kind": meta.get("source_kind"),
            "status": status,
            "message": message,
        }
    state["items"] = items
    return state


def _deal_rows(store="all", query=""):
    clauses = []
    params = []
    if store in {"lidl", "albert"}:
        clauses.append("store=?")
        params.append(store)
    if query:
        q = f"%{_norm(query)}%"
        clauses.append("(name_norm LIKE ? OR group_key LIKE ? OR lower(group_label) LIKE ?)")
        params.extend((q, q, f"%{query.lower()}%"))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM official_deals" + where + " ORDER BY group_label, price_value, name",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _row_visible(row, time_filter):
    today = date.today()
    start = None
    end = None
    try:
        start = date.fromisoformat(row["valid_from"]) if row.get("valid_from") else None
    except Exception:
        pass
    try:
        end = date.fromisoformat(row["valid_to"]) if row.get("valid_to") else None
    except Exception:
        pass

    if time_filter == "all":
        return not (end and end < today - timedelta(days=RETENTION_DAYS))
    if time_filter == "next":
        return bool(start and start > today)
    return not (start and start > today) and not (end and end < today)


def _serialize_row(row):
    status, message = _timing(row.get("valid_from"), row.get("valid_to"))
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "shop": row.get("store"),
        "store": row.get("store"),
        "price": row.get("price_text"),
        "price_text": row.get("price_text"),
        "original_price": row.get("original_price"),
        "discount_percent": row.get("discount_percent"),
        "amount": row.get("amount"),
        "valid_from": row.get("valid_from"),
        "valid_to": row.get("valid_to"),
        "source_url": row.get("source_url"),
        "source_kind": row.get("source_kind") or "official",
        "status": status,
        "status_message": message,
        "group_key": row.get("group_key"),
        "group_label": row.get("group_label"),
    }


def _fallback_rows(store, query):
    try:
        import shopping
        data = shopping.get_deals(force=False, query=query or None)
    except Exception:
        return []
    result = []
    for deal in data.get("deals", []):
        deal_store = deal.get("shop")
        if store in {"lidl", "albert"} and deal_store != store:
            continue
        name = _clean(deal.get("name"))
        if not name:
            continue
        group_key, group_label = _group_for(name)
        current = _float_price(deal.get("price"))
        original = _float_price(deal.get("original_price"))
        result.append({
            "id": None,
            "name": name,
            "shop": deal_store,
            "store": deal_store,
            "price": deal.get("price") or _fmt_price(current),
            "price_text": deal.get("price") or _fmt_price(current),
            "original_price": deal.get("original_price") or "",
            "discount_percent": deal.get("discount_percent") or _discount_percent(current, original, deal.get("amount", "")),
            "amount": deal.get("amount") or "",
            "valid_from": None,
            "valid_to": None,
            "source_url": deal.get("kupi_url") or "",
            "source_kind": "kupi-fallback",
            "status": "active",
            "status_message": "",
            "group_key": group_key,
            "group_label": group_label,
        })
    return result


def _sync_snapshot():
    with _SYNC_STATE_LOCK:
        state = dict(_SYNC_STATE)
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT store, source_url, fetched_at, status, last_error FROM official_sources ORDER BY store, source_url"
            ).fetchall()
        state["sources"] = [dict(row) for row in rows]
    except Exception:
        state["sources"] = []
    return state


def get_grouped_deals(store="all", time_filter="current", query=""):
    store = store if store in {"all", "lidl", "albert"} else "all"
    time_filter = time_filter if time_filter in {"current", "next", "all"} else "current"
    query = _clean(query)[:120]

    rows = [row for row in _deal_rows(store, query) if _row_visible(row, time_filter)]
    deals = [_serialize_row(row) for row in rows]

    if not deals and time_filter in {"current", "all"}:
        deals = _fallback_rows(store, query)

    groups = {}
    for deal in deals:
        key = deal.get("group_key") or "ostatni"
        group = groups.setdefault(key, {
            "key": key,
            "label": deal.get("group_label") or "Ostatní",
            "deals": [],
            "stores": set(),
            "prices": [],
        })
        group["deals"].append(deal)
        if deal.get("store") in {"lidl", "albert"}:
            group["stores"].add(deal["store"])
        price = _float_price(deal.get("price"))
        if price is not None:
            group["prices"].append(price)

    output = []
    for group in groups.values():
        group["deals"].sort(
            key=lambda d: (
                d.get("valid_from") or "",
                _float_price(d.get("price")) or 10**9,
                _norm(d.get("name")),
            )
        )
        output.append({
            "key": group["key"],
            "label": group["label"],
            "count": len(group["deals"]),
            "stores": sorted(group["stores"]),
            "from_price": _fmt_price(min(group["prices"])) if group["prices"] else "",
            "deals": group["deals"],
        })

    output.sort(key=lambda g: (_norm(g["label"]), g["key"]))
    return {
        "groups": output,
        "count": len(deals),
        "source": "official+fallback",
        "sync": _sync_snapshot(),
    }
