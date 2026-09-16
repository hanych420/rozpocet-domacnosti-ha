from __future__ import annotations

"""Runtime glue for the user's private/forked Parse.bot Albert API.

Haneva 0.11.1 uses the user's own Parse.bot API copy. The custom
`get_leaflet_products` endpoint already resolves the currently valid general
Hypermarket + Supermarket leaflets, therefore it must be called exactly once and
without leaflet/city parameters.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
import hashlib
import json

import shopping_integrations_v100 as source
import shopping_albert_v110 as albert
import shopping_official_base as core

_DEFAULT_PRODUCTS_ENDPOINT = "get_leaflet_products"
_ALBERT_REFRESH_HOURS = 24
_original_options = source._options


def _options_with_defaults():
    data = dict(_original_options() or {})
    data.setdefault("parse_albert_products_endpoint", _DEFAULT_PRODUCTS_ENDPOINT)
    return data


def _boolish(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = core._norm(value)
    if text in {"1", "true", "yes", "ano", "on"}:
        return True
    if text in {"0", "false", "no", "ne", "off", ""}:
        return False
    return default


def _configured_base(raw):
    """Accept either the API base or a copied full endpoint URL."""
    value = core._clean(raw).rstrip("/")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        path = parts.path.rstrip("/")
        for endpoint in (
            "get_leaflet_products",
            "get_current_leaflets",
            "get_leaflet_detail",
            "get_monthly_deals",
        ):
            suffix = "/" + endpoint
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")
    except Exception:
        return value


def _first(obj, keys):
    for key in keys:
        if key in obj and obj.get(key) not in (None, ""):
            return obj.get(key)
    return None


def _extract_structured_with_flags(payload, defaults=None, source_url=""):
    defaults = defaults or {}
    products = []
    for obj in source._walk(payload):
        if not isinstance(obj, dict):
            continue

        name = _first(obj, albert._NAME_KEYS)
        if not isinstance(name, str):
            continue
        name = core._clean(name)
        name_norm = core._norm(name)
        if len(name) < 3 or len(name) > 180 or any(bad in name_norm for bad in albert._BAD_TITLES):
            continue

        regular_price_raw = _first(obj, albert._PRICE_KEYS)
        if regular_price_raw in (None, ""):
            regular_price_raw = albert._price_from_structured_text(obj)
        regular_price = core._float_price(regular_price_raw)
        source_old = core._float_price(_first(obj, albert._OLD_PRICE_KEYS))
        app_price = core._float_price(_first(obj, ("app_price", "appPrice")))

        if regular_price is None and app_price is None:
            continue

        standard_price = None
        if app_price is not None and app_price > 0 and (regular_price is None or app_price < regular_price):
            current = app_price
            old = source_old if source_old is not None and source_old > current else regular_price
            standard_price = regular_price if regular_price is not None and regular_price > current else None
            explicit_app = True
        else:
            current = regular_price
            old = source_old
            explicit_app = _first(obj, ("appRequired", "app_required"))

        if current is None or current <= 0:
            continue

        context = albert._scalar_text(obj)
        inferred_app, inferred_activation, _ = albert._condition(context)
        explicit_activation = _first(obj, ("activationRequired", "activation_required"))
        explicit_condition = core._clean(_first(obj, ("conditionText", "condition_text")) or "")

        activation_required = (
            _boolish(explicit_activation) if explicit_activation is not None else inferred_activation
        )
        app_required = (
            _boolish(explicit_app) if explicit_app is not None else inferred_app
        ) or activation_required

        if activation_required:
            condition_text = "Nutno aktivovat v aplikaci Můj Albert"
        elif app_required:
            condition_text = "Cena platí s kartou Můj Albert"
        else:
            condition_text = explicit_condition

        if standard_price is not None:
            condition_text = (condition_text + f" · bez aplikace {core._fmt_price(standard_price)}").strip(" ·")

        leaflet_type = core._norm(_first(obj, ("leaflet_type", "leafletType")) or "")
        leaflet_label = "Hypermarket" if leaflet_type == "hypermarket" else "Supermarket" if leaflet_type == "supermarket" else ""
        if leaflet_label:
            condition_text = (condition_text + " · " + leaflet_label).strip(" ·")
        if explicit_condition and explicit_condition.lower() not in condition_text.lower():
            condition_text = (condition_text + " · " + explicit_condition).strip(" ·")

        valid_from = _first(obj, albert._DATE_FROM_KEYS) or defaults.get("valid_from")
        valid_to = _first(obj, albert._DATE_TO_KEYS) or defaults.get("valid_to")
        if not valid_from or not valid_to:
            try:
                parsed_from, parsed_to = core._date_range_from_text(context)
            except Exception:
                parsed_from, parsed_to = None, None
            valid_from = valid_from or parsed_from
            valid_to = valid_to or parsed_to

        item_source_url = core._clean(
            _first(obj, ("sourceUrl", "source_url", "url")) or source_url or defaults.get("view_url") or ""
        )
        amount = core._clean(
            _first(obj, ("package", "packageSize", "package_size") + albert._AMOUNT_KEYS) or ""
        )
        products.append({
            "name": name,
            "price_value": current,
            "original_price_value": old,
            "discount_percent": core._discount_percent(
                current, old, obj.get("discount_percent") or obj.get("discount") or obj.get("discountPercent") or context
            ),
            "amount": amount,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_url": item_source_url,
            "app_required": app_required,
            "activation_required": activation_required,
            "condition_text": condition_text,
        })
    return products


def _last_success_fresh():
    raw = source._meta_get("parse_albert_products_last_success")
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(raw)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - stamp < timedelta(hours=_ALBERT_REFRESH_HOURS)
    except Exception:
        return False


def _sync_with_user_parse_copy():
    opts = _options_with_defaults()
    api_key = str(opts.get("parse_api_key") or "").strip()
    if not api_key:
        source._log("Albert: chybí Parse.bot API klíč")
        return 0

    configured = _configured_base(opts.get("parse_api_base") or "")
    if not configured:
        source._log("Albert: chybí parse_api_base; vlož URL své Parse.bot API kopie")
        return 0

    existing = albert._existing_count()
    if existing and _last_success_fresh():
        source._log(f"Albert přes Parse.bot: poslední úspěšná aktualizace je mladší než {_ALBERT_REFRESH_HOURS} h, ponechávám {existing} nabídek")
        return existing

    endpoint = core._clean(opts.get("parse_albert_products_endpoint") or _DEFAULT_PRODUCTS_ENDPOINT)
    old_base = source.PARSE_BASE
    source.PARSE_BASE = configured
    try:
        # The custom endpoint already returns both current general HM and SM
        # leaflets. It has no input parameters and costs credits per successful
        # call, so Haneva must call it only once.
        payload = source._parse_get(endpoint, api_key)
        deals = _extract_structured_with_flags(payload, {}, "")
        if not deals:
            if existing:
                source._log(f"Albert: Parse.bot vrátil 0 použitelných položek, ponechávám {existing} uložených nabídek")
                return existing
            raise RuntimeError("Parse.bot get_leaflet_products nevrátil žádné použitelné produkty")

        fingerprint = hashlib.sha256(
            json.dumps(deals, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        count = albert._replace_albert(deals, fingerprint)
        source._meta_set("parse_albert_products_last_success", datetime.now(timezone.utc).isoformat())
        source._meta_set("parse_albert_products_fingerprint", fingerprint)
        activation = sum(1 for d in deals if d.get("activation_required"))
        app_only = sum(1 for d in deals if d.get("app_required") and not d.get("activation_required"))
        source._log(
            f"Albert přes Parse.bot: {count} nabídek z jednoho get_leaflet_products volání; "
            f"aktivace={activation}, karta/aplikace={app_only}"
        )
        return count
    finally:
        source.PARSE_BASE = old_base


source._options = _options_with_defaults
albert._extract_structured = _extract_structured_with_flags
albert._sync_albert_parse_v110 = _sync_with_user_parse_copy
source._sync_albert_parse = _sync_with_user_parse_copy
