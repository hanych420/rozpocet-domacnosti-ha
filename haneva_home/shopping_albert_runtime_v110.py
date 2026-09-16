from __future__ import annotations

"""Runtime glue for the user's private/forked Parse.bot Albert API.

The marketplace API is only the template. Once Parse.bot adds
`get_leaflet_products`, the endpoint lives on the user's own API copy, so Haneva
must use a configurable API base URL. This module also trusts the explicit
appRequired/activationRequired flags returned by that structured endpoint.
"""

from urllib.parse import urlsplit, urlunsplit

import shopping_integrations_v100 as source
import shopping_albert_v110 as albert
import shopping_official_base as core

_DEFAULT_PRODUCTS_ENDPOINT = "get_leaflet_products"
_original_options = source._options
_original_sync = albert._sync_albert_parse_v110


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

        price_raw = _first(obj, albert._PRICE_KEYS)
        if price_raw in (None, ""):
            price_raw = albert._price_from_structured_text(obj)
        current = core._float_price(price_raw)
        if current is None or current <= 0:
            continue

        old = core._float_price(_first(obj, albert._OLD_PRICE_KEYS))
        context = albert._scalar_text(obj)
        inferred_app, inferred_activation, _ = albert._condition(context)

        explicit_app = _first(obj, ("appRequired", "app_required"))
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

        # Keep a useful source condition as extra context without weakening the
        # clear UI warning above.
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
        products.append({
            "name": name,
            "price_value": current,
            "original_price_value": old,
            "discount_percent": core._discount_percent(
                current, old, obj.get("discount") or obj.get("discountPercent") or context
            ),
            "amount": core._clean(_first(obj, albert._AMOUNT_KEYS) or ""),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_url": item_source_url,
            "app_required": app_required,
            "activation_required": activation_required,
            "condition_text": condition_text,
        })
    return products


def _sync_with_user_parse_copy():
    opts = _options_with_defaults()
    configured = _configured_base(opts.get("parse_api_base") or "")
    old_base = source.PARSE_BASE
    if configured:
        source.PARSE_BASE = configured
    elif str(opts.get("parse_api_key") or "").strip():
        source._log(
            "Albert: API klíč je nastavený, ale parse_api_base chybí; po forku Parse.bot API vlož URL své kopie"
        )
    try:
        return _original_sync()
    finally:
        source.PARSE_BASE = old_base


source._options = _options_with_defaults
albert._extract_structured = _extract_structured_with_flags
albert._sync_albert_parse_v110 = _sync_with_user_parse_copy
source._sync_albert_parse = _sync_with_user_parse_copy
