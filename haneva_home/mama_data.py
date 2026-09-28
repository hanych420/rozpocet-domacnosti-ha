import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlparse


DB_PATH = "/data/haneva_mama.db"
MEDIA_DIR = "/data/haneva_mama_media"
MEDIA_MAX_BYTES = 12 * 1024 * 1024
MAX_WORKOUTS = 12
MAX_CATEGORIES = 12
MAX_EXERCISES = 30

def _exercise(exercise_id, name, art):
    return {"id": exercise_id, "name": name, "sets": 3, "reps": 10, "weight": "bez zátěže", "art": art, "image": ""}


DEFAULT_CATEGORIES = [
    {"id": "legs", "name": "Nohy", "art": "🦵", "exercises": [
        _exercise("legs-rumunsky-mrtvy-tah", "Rumunský mrtvý tah", "🏋️"),
        _exercise("legs-bridge", "Bridge na pravou a levou nohu", "🍑"),
        _exercise("legs-wall-sit", "Wall sit", "🧱"),
        _exercise("legs-extension", "Leg extension", "🦵"),
        _exercise("legs-bocni-vypady", "Boční výpady", "↔️"),
    ]},
    {"id": "arms", "name": "Ruce", "art": "💪", "exercises": [
        _exercise("arms-tlak-ramena", "Tlak na ramena", "🏋️"),
        _exercise("arms-kladivove-pritahy", "Kladivové přítahy", "💪"),
        _exercise("arms-kolem-sveta", "Kolem světa", "🌍"),
        _exercise("arms-tlak-nad-hlavou", "Tlak nad hlavou", "🙌"),
        _exercise("arms-kombinace", "Křídla / před sebe / dolů k bokům", "↕️"),
        _exercise("arms-biceps", "Bicepsový zdvih", "💪"),
    ]},
    {"id": "core", "name": "Břicho", "art": "🧘", "exercises": [
        _exercise("core-side-plank", "Side plank", "↔️"),
        _exercise("core-plank", "Plank", "🧘"),
        _exercise("core-mrtvy-brouk", "Mrtvý brouk", "🐞"),
        _exercise("core-rusky-twist", "Ruský twist", "🔄"),
    ]},
]

DEFAULT_PLANS = [
    {"id": "A", "categories": ["legs", "arms"], "excluded_exercises": []},
    {"id": "B", "categories": ["legs", "core"], "excluded_exercises": []},
]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
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
    legacy = re.fullmatch(r"/api/mama/media/([a-f0-9]{32}\.(?:gif|jpe?g|png|webp))", result)
    if legacy:
        return f"/mama/api/media/{legacy.group(1)}"
    if re.fullmatch(r"/mama/api/media/[a-f0-9]{32}\.(?:gif|jpe?g|png|webp)", result):
        return result
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Obrázek musí mít platnou adresu začínající http:// nebo https://.")
    return result


def _media_type(body):
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("Vyber fotku JPG, PNG, WebP nebo animovaný GIF.")


def save_media(original_name, body):
    if not body or len(body) > MEDIA_MAX_BYTES:
        raise ValueError("Fotka nebo GIF může mít nejvýše 12 MB.")
    mime, suffix = _media_type(body)
    filename = uuid.uuid4().hex + suffix
    Path(MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    (Path(MEDIA_DIR) / filename).write_bytes(body)
    return {
        "url": f"/mama/api/media/{filename}",
        "name": Path(original_name or ("napoveda" + suffix)).name[:180],
        "mime": mime,
    }


def read_media(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:gif|jpg|png|webp)", str(filename or "")):
        raise FileNotFoundError
    path = Path(MEDIA_DIR) / filename
    try:
        body = path.read_bytes()
    except OSError:
        raise FileNotFoundError
    mime, suffix = _media_type(body)
    if not filename.endswith(suffix):
        raise FileNotFoundError
    return body, mime


def validate_music_url(value):
    result = str(value or "").strip()
    if not result:
        return ""
    if len(result) > 500:
        raise ValueError("Adresa hudby je příliš dlouhá.")
    parsed = urlparse(result)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Hudba musí mít platnou adresu začínající https://.")
    allowed_hosts = {"open.spotify.com", "youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}
    if parsed.hostname not in allowed_hosts:
        raise ValueError("Použij odkaz na Spotify nebo YouTube.")
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


def _validated_exercise(item, index, category_id, used_ids):
    if not isinstance(item, dict):
        raise ValueError("Neplatný cvik.")
    exercise_id = _identifier(item.get("id"), f"{category_id}-cvik-{index + 1}")
    if exercise_id in used_ids:
        exercise_id = f"{exercise_id}-{index + 1}"
    used_ids.add(exercise_id)
    return {
        "id": exercise_id,
        "name": _text(item.get("name"), "Název cviku", 100),
        "sets": _number(item.get("sets"), "Počet sérií", 1, 10),
        "reps": _number(item.get("reps"), "Počet opakování", 1, 200),
        "weight": _text(item.get("weight"), "Váha", 60),
        "art": str(item.get("art") or "🏋️").strip()[:12] or "🏋️",
        "image": _image_url(item.get("image")),
    }


def validate_categories(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CATEGORIES:
        raise ValueError(f"Musí existovat 1 až {MAX_CATEGORIES} kategorií cviků.")
    categories = []
    category_ids = set()
    exercise_ids = set()
    for category_index, source in enumerate(raw):
        if not isinstance(source, dict):
            raise ValueError("Neplatná kategorie cviků.")
        category_id = _identifier(source.get("id"), f"kategorie-{category_index + 1}")
        if category_id in category_ids:
            raise ValueError("Každá kategorie musí být jedinečná.")
        category_ids.add(category_id)
        exercises_raw = source.get("exercises")
        if not isinstance(exercises_raw, list) or len(exercises_raw) > MAX_EXERCISES:
            raise ValueError(f"Kategorie může obsahovat nejvýše {MAX_EXERCISES} cviků.")
        categories.append({
            "id": category_id,
            "name": _text(source.get("name"), "Název kategorie", 60),
            "art": str(source.get("art") or "🏋️").strip()[:12] or "🏋️",
            "exercises": [
                _validated_exercise(item, exercise_index, category_id, exercise_ids)
                for exercise_index, item in enumerate(exercises_raw)
            ],
        })
    return categories


def validate_plans(raw, categories):
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_WORKOUTS:
        raise ValueError(f"Musí existovat 2 až {MAX_WORKOUTS} tréninkových dnů.")
    allowed = {category["id"] for category in categories}
    category_map = {category["id"]: category for category in categories}
    plans = []
    plan_ids = set()
    for plan_index, source in enumerate(raw):
        if not isinstance(source, dict):
            raise ValueError("Neplatný tréninkový den.")
        plan_id = _identifier(source.get("id"), chr(65 + plan_index))
        if plan_id in plan_ids:
            raise ValueError("Každý tréninkový den musí být jedinečný.")
        plan_ids.add(plan_id)
        selected = []
        for category_id in source.get("categories") or []:
            category_id = str(category_id)
            if category_id in allowed and category_id not in selected:
                selected.append(category_id)
        if not selected:
            raise ValueError(f"Trénink {plan_index + 1} musí obsahovat alespoň jednu kategorii.")
        available_exercises = {
            exercise["id"]
            for category_id in selected
            for exercise in category_map[category_id]["exercises"]
        }
        excluded = []
        for exercise_id in source.get("excluded_exercises") or []:
            exercise_id = str(exercise_id)
            if exercise_id in available_exercises and exercise_id not in excluded:
                excluded.append(exercise_id)
        if available_exercises and len(excluded) == len(available_exercises):
            raise ValueError(f"Trénink {plan_index + 1} musí obsahovat alespoň jeden cvik.")
        plans.append({"id": plan_id, "categories": selected, "excluded_exercises": excluded})
    return plans


def resolve_workouts(plans, categories):
    category_map = {category["id"]: category for category in categories}
    workouts = []
    for plan in plans:
        selected = [category_map[item] for item in plan["categories"] if item in category_map]
        excluded = set(plan.get("excluded_exercises") or [])
        exercises = []
        seen = set()
        for category in selected:
            for exercise in category["exercises"]:
                if exercise["id"] not in seen and exercise["id"] not in excluded:
                    exercises.append(dict(exercise))
                    seen.add(exercise["id"])
        if not exercises:
            raise ValueError("Každý trénink musí obsahovat alespoň jeden cvik.")
        workouts.append({
            "id": plan["id"],
            "name": " + ".join(category["name"] for category in selected),
            "exercises": exercises,
        })
    return workouts


def _legacy_to_library(workouts):
    workouts = validate_workouts(workouts)
    first, second = workouts[0], workouts[1]
    first_by_name = {item["name"].casefold(): item for item in first["exercises"]}
    second_by_name = {item["name"].casefold(): item for item in second["exercises"]}
    shared_names = set(first_by_name) & set(second_by_name)

    def migrated(category_id, source):
        result = []
        used = set()
        for index, item in enumerate(source):
            copy = dict(item)
            copy["id"] = _identifier(f"{category_id}-{item['name']}", f"{category_id}-cvik-{index + 1}")
            if copy["id"] in used:
                copy["id"] += f"-{index + 1}"
            used.add(copy["id"])
            if not copy.get("image"):
                other = second_by_name.get(item["name"].casefold())
                if other and other.get("image"):
                    copy["image"] = other["image"]
            result.append(copy)
        return result

    legs_source = [item for item in first["exercises"] if item["name"].casefold() in shared_names]
    arms_source = [item for item in first["exercises"] if item["name"].casefold() not in shared_names]
    core_source = [item for item in second["exercises"] if item["name"].casefold() not in shared_names]
    categories = validate_categories([
        {"id": "legs", "name": "Nohy", "art": "🦵", "exercises": migrated("legs", legs_source)},
        {"id": "arms", "name": "Ruce", "art": "💪", "exercises": migrated("arms", arms_source)},
        {"id": "core", "name": "Břicho", "art": "🧘", "exercises": migrated("core", core_source)},
    ])
    first_categories = (["legs"] if legs_source else []) + (["arms"] if arms_source else [])
    second_categories = (["legs"] if legs_source else []) + (["core"] if core_source else [])
    plans = validate_plans([
        {"id": first["id"], "categories": first_categories or ["arms"], "excluded_exercises": []},
        {"id": second["id"], "categories": second_categories or ["core"], "excluded_exercises": []},
    ], categories)
    return plans, categories


def _store_training_data(conn, plans, categories):
    for key, value in (
        ("training_plans", plans),
        ("exercise_categories", categories),
    ):
        conn.execute(
            """INSERT INTO mama_settings(key, value, updated_at)
               VALUES(?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
        )


def get_training_data():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        rows = dict(conn.execute(
            "SELECT key, value FROM mama_settings WHERE key IN ('training_plans','exercise_categories','workouts')"
        ).fetchall())
        try:
            categories = validate_categories(json.loads(rows["exercise_categories"]))
            plans = validate_plans(json.loads(rows["training_plans"]), categories)
            resolve_workouts(plans, categories)
            return plans, categories
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            plans, categories = _legacy_to_library(json.loads(rows["workouts"]))
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            categories = validate_categories(DEFAULT_CATEGORIES)
            plans = validate_plans(DEFAULT_PLANS, categories)
        _store_training_data(conn, plans, categories)
        conn.commit()
        return plans, categories


def get_workouts():
    plans, categories = get_training_data()
    return resolve_workouts(plans, categories)


def get_music_url():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM mama_settings WHERE key='music_url'").fetchone()
    if not row:
        return ""
    try:
        return validate_music_url(row[0])
    except ValueError:
        return ""


def save_music_url(raw):
    music_url = validate_music_url(raw)
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO mama_settings(key, value, updated_at)
               VALUES('music_url', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (music_url,),
        )
        conn.commit()
    return music_url


def get_music_shuffle():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM mama_settings WHERE key='music_shuffle'").fetchone()
    return bool(row and row[0] == "1")


def save_settings(raw_plans, raw_categories, raw_music_url, raw_music_shuffle=False):
    categories = validate_categories(raw_categories)
    plans = validate_plans(raw_plans, categories)
    workouts = resolve_workouts(plans, categories)
    music_url = validate_music_url(raw_music_url)
    music_shuffle = bool(raw_music_shuffle)
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _store_training_data(conn, plans, categories)
        conn.execute(
            """INSERT INTO mama_settings(key, value, updated_at)
               VALUES('music_url', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (music_url,),
        )
        conn.execute(
            """INSERT INTO mama_settings(key, value, updated_at)
               VALUES('music_shuffle', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            ("1" if music_shuffle else "0",),
        )
        conn.commit()
    return plans, categories, workouts, music_url, music_shuffle
