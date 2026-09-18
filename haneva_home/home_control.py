from __future__ import annotations

"""Home Assistant control bridge for Haneva Home.

The browser never receives the Supervisor token. Only explicitly whitelisted
entities can be read or controlled through this bridge.
"""

import os
import requests

HA_API_BASE = "http://supervisor/core/api"
TIMEOUT = 8

ENTITIES = {
    "switch.chodba": {"name": "Chodba", "room": "Chodba", "icon": "🚪"},
    "light.led_kuchyn": {"name": "LED kuchyň", "room": "Obývací pokoj", "icon": "✨"},
    "light.tv_svetla_2": {"name": "TV světla", "room": "Obývací pokoj", "icon": "📺"},
    "light.tz3000_oaq83gqc_ts0011_light": {"name": "Kuchyň", "room": "Obývací pokoj", "icon": "💡"},
    "fan.vetrak_k": {"name": "Větrák koupelna", "room": "Koupelna", "icon": "🌀"},
    "switch.on_off_light_2": {"name": "Světlo koupelna", "room": "Koupelna", "icon": "💡"},
    "light.svetlo": {"name": "Ložnice", "room": "Ložnice", "icon": "🛏️"},
    "light.extended_color_light_7": {"name": "LED skříň", "room": "Ložnice", "icon": "🌈"},
    "light.extended_color_light_1": {"name": "Pracovna", "room": "Ložnice", "icon": "🖥️"},
}

def _token():
    value=str(os.environ.get("SUPERVISOR_TOKEN") or "").strip()
    if not value: raise RuntimeError("Home Assistant Supervisor token není dostupný.")
    return value

def _headers():
    return {"Authorization":f"Bearer {_token()}","Content-Type":"application/json"}

def _allowed(entity_id):
    entity_id=str(entity_id or "").strip()
    if entity_id not in ENTITIES: raise ValueError("Tato entita není v Haneva Home povolená.")
    return entity_id

def _state_payload(entity_id,raw):
    cfg=ENTITIES[entity_id];state=str(raw.get("state") or "unknown");attrs=raw.get("attributes") or {}
    return {
        "entity_id":entity_id,"domain":entity_id.split(".",1)[0],"name":cfg["name"],"room":cfg["room"],"icon":cfg["icon"],
        "state":state,"is_on":state=="on","available":state not in {"unavailable","unknown",""},
        "friendly_name":attrs.get("friendly_name") or cfg["name"],"last_changed":raw.get("last_changed"),"last_updated":raw.get("last_updated"),
    }

def get_entity(entity_id):
    entity_id=_allowed(entity_id)
    response=requests.get(f"{HA_API_BASE}/states/{entity_id}",headers=_headers(),timeout=TIMEOUT)
    if response.status_code==404: raise KeyError("Entita v Home Assistantu nebyla nalezena.")
    response.raise_for_status()
    return _state_payload(entity_id,response.json())

def get_overview():
    items=[]
    for entity_id in ENTITIES:
        try: items.append(get_entity(entity_id))
        except Exception as exc:
            cfg=ENTITIES[entity_id]
            items.append({"entity_id":entity_id,"domain":entity_id.split(".",1)[0],"name":cfg["name"],"room":cfg["room"],"icon":cfg["icon"],"state":"unavailable","is_on":False,"available":False,"error":str(exc)})
    return {"entities":items}

def set_entity(entity_id,turn_on):
    entity_id=_allowed(entity_id);domain=entity_id.split(".",1)[0]
    if domain not in {"switch","light","fan"}: raise ValueError("Tento typ entity zatím Haneva Home neovládá.")
    service="turn_on" if bool(turn_on) else "turn_off"
    response=requests.post(f"{HA_API_BASE}/services/{domain}/{service}",headers=_headers(),json={"entity_id":entity_id},timeout=TIMEOUT)
    response.raise_for_status()
    return get_entity(entity_id)
