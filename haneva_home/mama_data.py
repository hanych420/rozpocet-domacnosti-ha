import json
import os
import re
import sqlite3
from urllib.parse import urlparse


DB_PATH = "/data/haneva_mama.db"
MAX_WORKOUTS = 12
MAX_EXERCISES = 30

DEFAULT_WORKOUTS = [
    {
        "id": "A",
        "name": "Nohy + ruce",
        "exercises": [
            {"id": "chair-squat", "name": "Dřep k židli", "sets": 3, "reps": 10, "weight": "bez zátěže", "art": "🪑", "image": ""},
            {"id": "biceps", "name": "Bicepsový zdvih", "sets": 3, "reps": 12, "weight": "2 kg", "art": "💪", "image": ""},
            {"id": "calf-raise", "name": "Výpony na špičky", "sets": 3, "reps": 12, "weight": "bez zátěže", "art": "🦵", "image": ""},
        ],
    },
    {
        "id": "B",
        "name": "Nohy + břicho",
        "exercises": [
            {"id": "step-back", "name": "Zakročení s oporou", "sets": 3, "reps": 8, "weight": "bez zátěže", "art": "🚶", "image": ""},
            {"id": "knee-lift", "name": "Zvedání kolen ve stoje", "sets": 3, "reps": 10, "weight": "bez zátěže", "art": "🧍", "image": ""},
            {"id": "wall-push", "name": "Kliky o zeď", "sets": 3, "reps": 10, "weight": "bez zátěže", "art": "🙌", "image": ""},
        ],
    },
]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS mama_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.commit()


def _text(value, label, maximum):
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{label} nesmí být prázdné.")
    if len(result) > maximum:
        raise ValueError(f"{label} je příliš dlouhé.")
    return result


def _number(value, label, minimum, maximum):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} musí být číslo.")
    if not minimum <= result <= maximum:
        raise ValueError(f"{label} musí být od {minimum} do {maximum}.")
    return result


def _identifier(value, fallback):
    result = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip()).strip("-")[:48]
    return result or fallback


def _image_url(value):
    result = str(value or "").strip()
    if not result:
        return ""
    if len(result) > 500:
        raise ValueError("Adresa obrázku je příliš dlouhá.")
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Obrázek musí mít platnou adresu začínající http:// nebo https://.")
    return result


def validate_workouts(raw):
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_WORKOUTS:
        raise ValueError(f"Musí existovat 2 až {MAX_WORKOUTS} tréninkových dnů.")

    workouts = []
    workout_ids = set()
    for workout_index, source in enumerate(raw):
        if not isinstance(source, dict):
            raise ValueError("Neplatný tréninkový den.")
        workout_id = _identifier(source.get("id"), chr(65 + workout_index))
        if workout_id in workout_ids:
            workout_id = f"{workout_id}-{workout_index + 1}"
        workout_ids.add(workout_id)
        exercises_raw = source.get("exercises")
        if not isinstance(exercises_raw, list) or not 1 <= len(exercises_raw) <= MAX_EXERCISES:
            raise ValueError(f"Každý trénink musí mít 1 až {MAX_EXERCISES} cviků.")

        exercise_ids = set()
        exercises = []
        for exercise_index, item in enumerate(exercises_raw):
            if not isinstance(item, dict):
                raise ValueError("Neplatný cvik.")
            exercise_id = _identifier(item.get("id"), f"cvik-{exercise_index + 1}")
            if exercise_id in exercise_ids:
                exercise_id = f"{exercise_id}-{exercise_index + 1}"
            exercise_ids.add(exercise_id)
            art = str(item.get("art") or "🏋️").strip()[:12] or "🏋️"
            exercises.append({
                "id": exercise_id,
                "name": _text(item.get("name"), "Název cviku", 100),
                "sets": _number(item.get("sets"), "Počet sérií", 1, 10),
                "reps": _number(item.get("reps"), "Počet opakování", 1, 200),
                "weight": _text(item.get("weight"), "Váha", 60),
                "art": art,
                "image": _image_url(item.get("image")),
            })
        workouts.append({
            "id": workout_id,
            "name": _text(source.get("name"), "Název tréninku", 80),
            "exercises": exercises,
        })
    return workouts


def get_workouts():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM mama_settings WHERE key='workouts'").fetchone()
    if row:
        try:
            return validate_workouts(json.loads(row[0]))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return validate_workouts(DEFAULT_WORKOUTS)


def save_workouts(raw):
    workouts = validate_workouts(raw)
    init_db()
    value = json.dumps(workouts, ensure_ascii=False, separators=(",", ":"))
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO mama_settings(key, value, updated_at)
               VALUES('workouts', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (value,),
        )
        conn.commit()
    return workouts
