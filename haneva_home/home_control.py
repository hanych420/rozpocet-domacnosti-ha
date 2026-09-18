from __future__ import annotations

"""Home Assistant control bridge for Haneva Home.

The browser never receives the Supervisor token. Configured entities are stored
locally in /data and only light/switch/fan domains can be added.
"""

import json
import os
import re
import sqlite3

import requests

HA_API_BASE = "http://supervisor/core/api"
TIMEOUT = 8
DB_PATH = "/data/haneva_home_control.db"
ALLOWED_DOMAINS = {"light", "switch", "fan"}
ENTITY_RE = re.compile(r"^(light|switch|fan)\.[a-z0-9_]+$")

DEFAULT_ENTITIES = [
    ("switch.chodba", "Chodba", "Chodba", "🚪"),
    ("light.led_kuchyn", "LED kuchyň", "Obývací pokoj", "✨"),
    ("light.tv_svetla_2", "TV světla", "Obývací pokoj", "📺"),
    ("light.tz3000_oaq83gqc_ts0011_light", "Kuchyň", "Obývací pokoj", "💡"),
    ("fan.vetrak_k", "Větrák koupelna", "Koupelna", "🌀"),
    ("switch.on_off_light_2", "Světlo koupelna", "Koupelna", "💡"),
    ("light.svetlo", "Ložnice", "Ložnice", "🛏️"),
    ("light.extended_color_light_7", "LED skříň", "Ložnice", "🌈"),
    ("light.extended_color_light_1", "Pracovna", "Ložnice", "🖥️"),
]


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                room TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '💡',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        seeded = conn.execute(
            "SELECT value FROM meta WHERE key='defaults_seeded_v1'"
        ).fetchone()
        if not seeded:
            for pos, (entity_id, name, room, icon) in enumerate(DEFAULT_ENTITIES):
                conn.execute(
                    """INSERT OR IGNORE INTO entities(entity_id,name,room,icon,position)
                       VALUES(?,?,?,?,?)""",
                    (entity_id, name, room, icon, pos),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('defaults_seeded_v1','1')"
            )
        conn.commit()


def _token():
    value = str(os.environ.get("SUPERVISOR_TOKEN") or "").strip()
    if not value:
        raise RuntimeError("Home Assistant Supervisor token není dostupný.")
    return value


def _headers():
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _raw_state(entity_id):
    response = requests.get(
        f"{HA_API_BASE}/states/{entity_id}",
        headers=_headers(),
        timeout=TIMEOUT,
    )
    if response.status_code == 404:
        raise KeyError("Entita v Home Assistantu nebyla nalezena.")
    response.raise_for_status()
    return response.json()


def _config(entity_id):
    init_db()
    with _db() as conn:
        row = conn.execute(
            "SELECT entity_id,name,room,icon,position FROM entities WHERE entity_id=?",
            (str(entity_id or "").strip(),),
        ).fetchone()
    if not row:
        raise ValueError("Tato entita není v Haneva Home povolená.")
    return dict(row)


def _state_payload(cfg, raw):
    entity_id = cfg["entity_id"]
    domain = entity_id.split(".", 1)[0]
    state = str(raw.get("state") or "unknown")
    attrs = raw.get("attributes") or {}
    modes = [str(x) for x in (attrs.get("supported_color_modes") or [])]
    brightness = attrs.get("brightness")
    try:
        brightness_pct = round(float(brightness) / 255 * 100) if brightness is not None else None
    except Exception:
        brightness_pct = None
    color_modes = {"hs", "xy", "rgb", "rgbw", "rgbww"}
    supports_color = domain == "light" and bool(color_modes.intersection(modes))
    supports_brightness = domain == "light" and (
        brightness is not None or bool(set(modes) - {"onoff"})
    )
    rgb = attrs.get("rgb_color")
    if not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
        rgb = None
    else:
        try:
            rgb = [max(0, min(255, int(rgb[0]))), max(0, min(255, int(rgb[1]))), max(0, min(255, int(rgb[2])))]
        except Exception:
            rgb = None

    return {
        "entity_id": entity_id,
        "domain": domain,
        "name": cfg["name"],
        "room": cfg["room"],
        "icon": cfg["icon"],
        "position": int(cfg.get("position") or 0),
        "state": state,
        "is_on": state == "on",
        "available": state not in {"unavailable", "unknown", ""},
        "friendly_name": attrs.get("friendly_name") or cfg["name"],
        "brightness_pct": brightness_pct,
        "supports_brightness": supports_brightness,
        "supports_color": supports_color,
        "supported_color_modes": modes,
        "rgb_color": rgb,
        "color_temp_kelvin": attrs.get("color_temp_kelvin"),
        "last_changed": raw.get("last_changed"),
        "last_updated": raw.get("last_updated"),
    }


def get_config():
    init_db()
    with _db() as conn:
        rows = conn.execute(
            "SELECT entity_id,name,room,icon,position FROM entities ORDER BY position,id"
            .replace(",id", ",entity_id")
        ).fetchall()
    return {"entities": [dict(row) for row in rows]}


def get_entity(entity_id):
    cfg = _config(entity_id)
    return _state_payload(cfg, _raw_state(cfg["entity_id"]))


def get_overview():
    init_db()
    with _db() as conn:
        rows = conn.execute(
            "SELECT entity_id,name,room,icon,position FROM entities ORDER BY position,entity_id"
        ).fetchall()
    items = []
    for row in rows:
        cfg = dict(row)
        try:
            items.append(_state_payload(cfg, _raw_state(cfg["entity_id"])))
        except Exception as exc:
            entity_id = cfg["entity_id"]
            items.append({
                **cfg,
                "domain": entity_id.split(".", 1)[0],
                "state": "unavailable",
                "is_on": False,
                "available": False,
                "supports_brightness": False,
                "supports_color": False,
                "supported_color_modes": [],
                "rgb_color": None,
                "brightness_pct": None,
                "error": str(exc),
            })
    return {"entities": items}


def add_entity(payload):
    init_db()
    entity_id = str(payload.get("entity_id") or "").strip().lower()
    if not ENTITY_RE.fullmatch(entity_id):
        raise ValueError("Použij ID entity typu light.xxx, switch.xxx nebo fan.xxx.")

    raw = _raw_state(entity_id)
    attrs = raw.get("attributes") or {}
    domain = entity_id.split(".", 1)[0]
    name = str(payload.get("name") or attrs.get("friendly_name") or entity_id).strip()[:80]
    room = str(payload.get("room") or "Ostatní").strip()[:80] or "Ostatní"
    default_icon = "🌀" if domain == "fan" else "💡" if domain == "light" else "🔘"
    icon = str(payload.get("icon") or default_icon).strip()[:12] or default_icon

    with _db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        if exists:
            conn.execute(
                """UPDATE entities
                   SET name=?,room=?,icon=?,updated_at=CURRENT_TIMESTAMP
                   WHERE entity_id=?""",
                (name, room, icon, entity_id),
            )
        else:
            row = conn.execute("SELECT COALESCE(MAX(position),-1)+1 AS p FROM entities").fetchone()
            position = int(row["p"] if row else 0)
            conn.execute(
                """INSERT INTO entities(entity_id,name,room,icon,position)
                   VALUES(?,?,?,?,?)""",
                (entity_id, name, room, icon, position),
            )
        conn.commit()
    return get_entity(entity_id)


def remove_entity(entity_id):
    init_db()
    entity_id = str(entity_id or "").strip().lower()
    with _db() as conn:
        cur = conn.execute("DELETE FROM entities WHERE entity_id=?", (entity_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise KeyError("Zařízení nebylo nalezeno.")
    return True


def set_entity(entity_id, turn_on=None, brightness_pct=None, rgb_color=None):
    cfg = _config(entity_id)
    entity_id = cfg["entity_id"]
    domain = entity_id.split(".", 1)[0]
    if domain not in ALLOWED_DOMAINS:
        raise ValueError("Tento typ entity Haneva neovládá.")

    service_data = {"entity_id": entity_id}
    has_adjustment = False

    if brightness_pct is not None:
        if domain != "light":
            raise ValueError("Jas lze měnit pouze u světel.")
        try:
            pct = int(round(float(brightness_pct)))
        except Exception:
            raise ValueError("Neplatná hodnota jasu.")
        service_data["brightness_pct"] = max(1, min(100, pct))
        has_adjustment = True

    if rgb_color is not None:
        if domain != "light":
            raise ValueError("Barvu lze měnit pouze u světel.")
        if isinstance(rgb_color, str):
            value = rgb_color.strip().lstrip("#")
            if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
                raise ValueError("Neplatná barva.")
            rgb_color = [int(value[i:i+2], 16) for i in (0, 2, 4)]
        if not isinstance(rgb_color, (list, tuple)) or len(rgb_color) < 3:
            raise ValueError("Neplatná barva.")
        try:
            service_data["rgb_color"] = [
                max(0, min(255, int(rgb_color[0]))),
                max(0, min(255, int(rgb_color[1]))),
                max(0, min(255, int(rgb_color[2]))),
            ]
        except Exception:
            raise ValueError("Neplatná barva.")
        has_adjustment = True

    if has_adjustment:
        service = "turn_on"
    elif turn_on is None:
        raise ValueError("Chybí požadovaná změna zařízení.")
    else:
        service = "turn_on" if bool(turn_on) else "turn_off"

    response = requests.post(
        f"{HA_API_BASE}/services/{domain}/{service}",
        headers=_headers(),
        json=service_data,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return get_entity(entity_id)
