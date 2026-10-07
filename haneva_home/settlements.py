from datetime import date, datetime
from pathlib import Path
import sqlite3


DB_PATH = "/data/haneva_v1.db"
PERSONS = {"hanych": "Hanych", "eva": "Eva"}
ENTRY_TYPES = {"expense", "settlement"}
MONTH_NAMES = ("", "leden", "únor", "březen", "duben", "květen", "červen", "červenec", "srpen", "září", "říjen", "listopad", "prosinec")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settlement_entries(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entry_type TEXT NOT NULL CHECK(entry_type IN ('expense','settlement')),
          paid_by TEXT NOT NULL CHECK(paid_by IN ('hanych','eva')),
          amount REAL NOT NULL CHECK(amount > 0),
          title TEXT NOT NULL DEFAULT '',
          occurred_on TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_settlement_entries_date
          ON settlement_entries(occurred_on, id);
        """)
        conn.commit()


def _clean_text(value, limit):
    return " ".join(str(value or "").strip().split())[:limit]


def _valid_date(value):
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("Vyber platné datum.") from exc


def _valid_month(value):
    text = str(value or "").strip()
    if not text:
        return date.today().strftime("%Y-%m")
    try:
        datetime.strptime(text, "%Y-%m")
    except ValueError as exc:
        raise ValueError("Období musí být ve formátu RRRR-MM.") from exc
    return text


def _valid_amount(value):
    try:
        amount = round(float(str(value).replace(" ", "").replace(",", ".")), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("Zadej platnou částku.") from exc
    if amount <= 0 or amount > 10_000_000:
        raise ValueError("Částka musí být větší než 0 Kč.")
    return amount


def _entry_dict(row):
    item = dict(row)
    item["amount"] = round(float(item["amount"]), 2)
    item["paid_by_name"] = PERSONS[item["paid_by"]]
    item["received_by_name"] = PERSONS["eva" if item["paid_by"] == "hanych" else "hanych"]
    item["balance_change"] = _balance_change(item)
    return item


def _balance_change(entry):
    amount = float(entry["amount"])
    # Kladná hodnota znamená, že Hanych má dostat od Evy.
    direction = 1 if entry["paid_by"] == "hanych" else -1
    return round(direction * (amount / 2 if entry["entry_type"] == "expense" else amount), 2)


def save_entry(payload, entry_id=None):
    if not isinstance(payload, dict):
        raise ValueError("Záznam má neplatný formát.")
    current = None
    if entry_id is not None:
        with db() as conn:
            current = conn.execute("SELECT * FROM settlement_entries WHERE id=?", (entry_id,)).fetchone()
        if not current:
            raise KeyError("Záznam nebyl nalezen.")
        current = dict(current)

    def value(key, default=""):
        if key in payload:
            return payload[key]
        return current[key] if current else default

    entry_type = str(value("entry_type", "expense") or "").strip().lower()
    if entry_type not in ENTRY_TYPES:
        raise ValueError("Vyber platný typ záznamu.")
    paid_by = str(value("paid_by") or "").strip().lower()
    if paid_by not in PERSONS:
        raise ValueError("Vyber, kdo platil.")
    amount = _valid_amount(value("amount"))
    occurred_on = _valid_date(value("occurred_on", date.today().isoformat()))
    title = _clean_text(value("title"), 160)
    note = _clean_text(value("note"), 500)
    if entry_type == "expense" and not title:
        raise ValueError("Napiš, za co se platilo.")
    if entry_type == "settlement" and not title:
        title = "Vyrovnání"

    with db() as conn:
        if current:
            conn.execute(
                """UPDATE settlement_entries
                   SET entry_type=?,paid_by=?,amount=?,title=?,occurred_on=?,note=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (entry_type, paid_by, amount, title, occurred_on, note, entry_id),
            )
            saved_id = entry_id
        else:
            cur = conn.execute(
                """INSERT INTO settlement_entries(entry_type,paid_by,amount,title,occurred_on,note)
                   VALUES(?,?,?,?,?,?)""",
                (entry_type, paid_by, amount, title, occurred_on, note),
            )
            saved_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM settlement_entries WHERE id=?", (saved_id,)).fetchone()
    return _entry_dict(row)


def delete_entry(entry_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM settlement_entries WHERE id=?", (entry_id,))
        conn.commit()
    if not cur.rowcount:
        raise KeyError("Záznam nebyl nalezen.")


def _balance_message(balance):
    amount = round(abs(balance), 2)
    if amount < 0.01:
        return {"from": "", "to": "", "amount": 0, "text": "Máte vyrovnáno."}
    if balance > 0:
        return {"from": "Eva", "to": "Hanych", "amount": amount, "text": f"Eva vyrovná Hanychovi {amount:g} Kč."}
    return {"from": "Hanych", "to": "Eva", "amount": amount, "text": f"Hanych vyrovná Evě {amount:g} Kč."}


def summary(month=None):
    month = _valid_month(month)
    start = date.fromisoformat(month + "-01")
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    with db() as conn:
        all_rows = conn.execute(
            "SELECT * FROM settlement_entries ORDER BY occurred_on,id"
        ).fetchall()
        month_rows = conn.execute(
            """SELECT * FROM settlement_entries
               WHERE occurred_on>=? AND occurred_on<? ORDER BY occurred_on DESC,id DESC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        month_values = conn.execute(
            """SELECT paid_by,COALESCE(SUM(amount),0) total
               FROM settlement_entries
               WHERE entry_type='expense' AND occurred_on>=? AND occurred_on<? GROUP BY paid_by""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        available = [row[0] for row in conn.execute(
            "SELECT DISTINCT substr(occurred_on,1,7) FROM settlement_entries ORDER BY 1"
        ).fetchall()]
        monthly_rows = conn.execute(
            """SELECT substr(occurred_on,1,7) month,paid_by,COALESCE(SUM(amount),0) total
               FROM settlement_entries WHERE entry_type='expense'
               GROUP BY substr(occurred_on,1,7),paid_by ORDER BY month"""
        ).fetchall()

    balance = round(sum(_balance_change(row) for row in all_rows), 2)
    paid = {"hanych": 0.0, "eva": 0.0}
    for row in month_values:
        paid[row["paid_by"]] = round(float(row["total"]), 2)
    if month not in available:
        available.append(month)
    available.sort()
    month_labels = []
    for value in available:
        year, month_number = map(int, value.split("-"))
        month_labels.append({"value": value, "label": f"{MONTH_NAMES[month_number]} {year}"})
    series = {}
    for row in monthly_rows:
        series.setdefault(row["month"], {"month": row["month"], "hanych": 0.0, "eva": 0.0})
        series[row["month"]][row["paid_by"]] = round(float(row["total"]), 2)
    return {
        "month": month,
        "available_months": month_labels,
        "entries": [_entry_dict(row) for row in month_rows],
        "paid": paid,
        "personal_expenses": round(paid["hanych"] + paid["eva"], 2),
        "balance": balance,
        "balance_status": _balance_message(balance),
        "monthly_series": list(series.values()),
    }
