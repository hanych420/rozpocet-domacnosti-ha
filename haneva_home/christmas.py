from datetime import datetime
from io import BytesIO
from pathlib import Path
from contextlib import contextmanager
import os
import re
import sqlite3
import uuid

from PIL import Image, ImageOps


DB_PATH = os.environ.get("HANEVA_CHRISTMAS_DB_PATH", "/data/haneva_christmas.db")
IMAGE_DIR = os.environ.get("HANEVA_CHRISTMAS_IMAGE_DIR", "/data/haneva_christmas_images")
IMAGE_MAX_BYTES = 12 * 1024 * 1024

USERS = {
    "tatinek": {"name": "Tatínek", "short": "Táta", "emoji": "🎄"},
    "maminka": {"name": "Maminka", "short": "Máma", "emoji": "⭐"},
    "lukasek": {"name": "Lukášek", "short": "Lukášek", "emoji": "🦌"},
    "honzicek": {"name": "Honzíček", "short": "Honzíček", "emoji": "🎁"},
}

RECIPIENTS = {
    "tatinek": {"name": "Tatínek", "members": ["tatinek"], "emoji": "🎄"},
    "maminka": {"name": "Maminka", "members": ["maminka"], "emoji": "⭐"},
    "lukasek": {"name": "Lukášek", "members": ["lukasek"], "emoji": "🦌"},
    "honzicek": {"name": "Honzíček", "members": ["honzicek"], "emoji": "🎁"},
    "babicka": {"name": "Babička", "members": [], "emoji": "🕯️"},
    "dedecek": {"name": "Dědeček", "members": [], "emoji": "❄️"},
    "rodice": {"name": "Maminka a Tatínek", "members": ["maminka", "tatinek"], "emoji": "🏠"},
    "prarodice": {"name": "Babička a Dědeček", "members": [], "emoji": "❤️"},
}


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(IMAGE_DIR).mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wishes(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              recipient_key TEXT NOT NULL,
              title TEXT NOT NULL,
              image_name TEXT NOT NULL DEFAULT '',
              price REAL,
              desire_level INTEGER NOT NULL DEFAULT 2,
              purchase_url TEXT NOT NULL DEFAULT '',
              notes TEXT NOT NULL DEFAULT '',
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reservations(
              wish_id INTEGER PRIMARY KEY,
              reserved_by TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(wish_id) REFERENCES wishes(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_christmas_wishes_recipient
              ON wishes(recipient_key, desire_level, created_at);
            """
        )
        conn.commit()


def clean(value, limit=500):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def normalize_user(value):
    value = clean(value, 40).lower()
    if value not in USERS:
        raise ValueError("Nejdřív vyber, kdo právě používá vánoční seznam.")
    return value


def _recipient(value):
    value = clean(value, 40).lower()
    if value not in RECIPIENTS:
        raise ValueError("Vyber platného obdarovaného.")
    return value


def _price(value):
    if value is None or not str(value).strip():
        return None
    try:
        price = float(str(value).replace(" ", "").replace(",", "."))
    except ValueError as exc:
        raise ValueError("Cena musí být číslo.") from exc
    if price < 0 or price > 10_000_000:
        raise ValueError("Cena je mimo povolený rozsah.")
    return round(price, 2)


def _desire(value):
    try:
        level = int(value or 2)
    except (TypeError, ValueError):
        level = 2
    if level not in {1, 2, 3}:
        raise ValueError("Vyber platnou míru přání.")
    return level


def _wish_row(conn, wish_id):
    row = conn.execute("SELECT * FROM wishes WHERE id=?", (int(wish_id),)).fetchone()
    if not row:
        raise KeyError("Přání nebylo nalezeno.")
    return row


def _can_manage(row, actor):
    return actor == row["created_by"] and row["recipient_key"] == actor


def _can_delete(row, actor):
    # The author may remove an older cross-list entry created before ownership
    # enforcement was introduced, but cannot edit it or add another one.
    return actor == row["created_by"]


def _serialize(row, actor, reservation=None):
    item = dict(row)
    recipient = RECIPIENTS[item["recipient_key"]]
    is_recipient = actor in recipient["members"]
    item["image_url"] = (
        f"/api/v1/christmas/images/{item['image_name']}" if item.get("image_name") else ""
    )
    item.pop("image_name", None)
    item.pop("reserved_by", None)
    item.pop("reservation_created_at", None)
    item["is_recipient"] = is_recipient
    item["can_edit"] = _can_manage(row, actor)
    item["can_delete"] = _can_delete(row, actor)
    item["reservation_visible"] = not is_recipient
    item["can_reserve"] = not is_recipient
    if not is_recipient:
        item["reservation"] = (
            {
                "reserved_by": reservation["reserved_by"],
                "reserved_by_name": USERS[reservation["reserved_by"]]["name"],
                "is_mine": reservation["reserved_by"] == actor,
            }
            if reservation
            else None
        )
    return item


def state(actor):
    actor = normalize_user(actor)
    with db() as conn:
        rows = conn.execute(
            """SELECT w.*,r.reserved_by,r.created_at AS reservation_created_at
               FROM wishes w LEFT JOIN reservations r ON r.wish_id=w.id
               ORDER BY w.desire_level ASC,w.created_at DESC,w.id DESC"""
        ).fetchall()
    groups = []
    for key, recipient in RECIPIENTS.items():
        wishes = []
        for row in rows:
            if row["recipient_key"] != key:
                continue
            reservation = {"reserved_by": row["reserved_by"]} if row["reserved_by"] else None
            wishes.append(_serialize(row, actor, reservation))
        groups.append(
            {
                "key": key,
                "name": recipient["name"],
                "emoji": recipient["emoji"],
                "is_mine": actor in recipient["members"],
                "can_add": key == actor,
                "wishes": wishes,
            }
        )
    return {
        "user": {"key": actor, **USERS[actor]},
        "users": [{"key": key, **value} for key, value in USERS.items()],
        "groups": groups,
        "year": datetime.now().year,
    }


def save_wish(payload, actor, wish_id=None):
    actor = normalize_user(actor)
    title = clean(payload.get("title"), 200)
    if not title:
        raise ValueError("Název přání je povinný.")
    recipient_key = _recipient(payload.get("recipient_key"))
    if recipient_key != actor:
        raise PermissionError("Přání můžeš přidat jen do svého vlastního seznamu.")
    purchase_url = clean(payload.get("purchase_url"), 1500)
    if purchase_url and not re.match(r"^https?://", purchase_url, re.I):
        raise ValueError("Odkaz musí začínat http:// nebo https://.")
    notes = clean(payload.get("notes"), 2000)
    price = _price(payload.get("price"))
    desire_level = _desire(payload.get("desire_level"))

    with db() as conn:
        if wish_id is not None:
            existing = _wish_row(conn, wish_id)
            if not _can_manage(existing, actor):
                raise PermissionError("Upravit můžeš jen své přání ve vlastním seznamu.")
            conn.execute(
                """UPDATE wishes SET recipient_key=?,title=?,price=?,desire_level=?,
                   purchase_url=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (recipient_key, title, price, desire_level, purchase_url, notes, int(wish_id)),
            )
            item_id = int(wish_id)
        else:
            cursor = conn.execute(
                """INSERT INTO wishes(recipient_key,title,price,desire_level,purchase_url,notes,created_by)
                   VALUES(?,?,?,?,?,?,?)""",
                (recipient_key, title, price, desire_level, purchase_url, notes, actor),
            )
            item_id = cursor.lastrowid
        conn.commit()
        row = _wish_row(conn, item_id)
        reservation = conn.execute(
            "SELECT reserved_by FROM reservations WHERE wish_id=?", (item_id,)
        ).fetchone()
    return _serialize(row, actor, reservation)


def delete_wish(wish_id, actor):
    actor = normalize_user(actor)
    image_name = ""
    with db() as conn:
        row = _wish_row(conn, wish_id)
        if not _can_delete(row, actor):
            raise PermissionError("Smazat můžeš jen přání, které jsi přidal(a).")
        image_name = row["image_name"]
        conn.execute("DELETE FROM wishes WHERE id=?", (int(wish_id),))
        conn.commit()
    if image_name:
        try:
            (Path(IMAGE_DIR) / image_name).unlink(missing_ok=True)
        except OSError:
            pass


def set_reservation(wish_id, actor, reserve=True):
    actor = normalize_user(actor)
    with db() as conn:
        row = _wish_row(conn, wish_id)
        if actor in RECIPIENTS[row["recipient_key"]]["members"]:
            raise PermissionError("U vlastního přání nelze zobrazit ani měnit rezervaci.")
        current = conn.execute(
            "SELECT reserved_by FROM reservations WHERE wish_id=?", (int(wish_id),)
        ).fetchone()
        if reserve:
            if current and current["reserved_by"] != actor:
                raise ValueError(f"Toto přání už zajišťuje {USERS[current['reserved_by']]['name']}.")
            conn.execute(
                "INSERT OR IGNORE INTO reservations(wish_id,reserved_by) VALUES(?,?)",
                (int(wish_id), actor),
            )
            current = conn.execute(
                "SELECT reserved_by FROM reservations WHERE wish_id=?", (int(wish_id),)
            ).fetchone()
            if not current or current["reserved_by"] != actor:
                owner = USERS[current["reserved_by"]]["name"] if current else "někdo jiný"
                raise ValueError(f"Toto přání už zajišťuje {owner}.")
            conn.execute(
                "UPDATE reservations SET updated_at=CURRENT_TIMESTAMP WHERE wish_id=?",
                (int(wish_id),),
            )
        else:
            if current and current["reserved_by"] != actor:
                raise PermissionError("Rezervaci může zrušit jen ten, kdo ji vytvořil.")
            conn.execute("DELETE FROM reservations WHERE wish_id=?", (int(wish_id),))
        conn.commit()
    return state(actor)


def save_wish_image(wish_id, actor, original_name, body):
    actor = normalize_user(actor)
    if not body or len(body) > IMAGE_MAX_BYTES:
        raise ValueError("Fotka chybí nebo je větší než 12 MB.")
    with db() as conn:
        row = _wish_row(conn, wish_id)
        if not _can_manage(row, actor):
            raise PermissionError("Fotku můžeš změnit jen u svého přání ve vlastním seznamu.")
        old_name = row["image_name"]

    Path(IMAGE_DIR).mkdir(parents=True, exist_ok=True)
    image_name = f"{uuid.uuid4().hex}.webp"
    target = Path(IMAGE_DIR) / image_name
    try:
        with Image.open(BytesIO(body)) as image:
            image.load()
            image = ImageOps.exif_transpose(image)
            if image.width < 64 or image.height < 64:
                raise ValueError("Fotka je příliš malá.")
            if image.width * image.height > 40_000_000:
                raise ValueError("Fotka má příliš vysoké rozlišení.")
            image = image.convert("RGB")
            image.thumbnail((1600, 1600), getattr(Image, "Resampling", Image).LANCZOS)
            image.save(target, "WEBP", quality=88, method=4)
    except ValueError:
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise ValueError("Soubor není podporovaná fotografie.") from exc

    with db() as conn:
        conn.execute(
            "UPDATE wishes SET image_name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (image_name, int(wish_id)),
        )
        conn.commit()
        saved = _wish_row(conn, wish_id)
        reservation = conn.execute(
            "SELECT reserved_by FROM reservations WHERE wish_id=?", (int(wish_id),)
        ).fetchone()
    if old_name and old_name != image_name:
        try:
            (Path(IMAGE_DIR) / old_name).unlink(missing_ok=True)
        except OSError:
            pass
    return _serialize(saved, actor, reservation)


def read_image(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.webp", str(filename or "")):
        raise FileNotFoundError(filename)
    path = Path(IMAGE_DIR) / filename
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path.read_bytes(), "image/webp"

