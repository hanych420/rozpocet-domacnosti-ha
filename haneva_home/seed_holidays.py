from datetime import date, timedelta

import app

SYSTEM_NOTE = "Automaticky přidaný den pracovního klidu v ČR."
ANCHOR_YEAR = 2026
EASTER_START_YEAR = 2020
EASTER_END_YEAR = 2100

FIXED_HOLIDAYS = [
    (1, 1, "🇨🇿 Nový rok / Den obnovy ČR"),
    (5, 1, "🇨🇿 Svátek práce"),
    (5, 8, "🇨🇿 Den vítězství"),
    (7, 5, "🇨🇿 Cyril a Metoděj"),
    (7, 6, "🇨🇿 Jan Hus"),
    (9, 28, "🇨🇿 Den české státnosti"),
    (10, 28, "🇨🇿 Den vzniku Československa"),
    (11, 17, "🇨🇿 17. listopad"),
    (12, 24, "🎄 Štědrý den"),
    (12, 25, "🎄 1. svátek vánoční"),
    (12, 26, "🎄 2. svátek vánoční"),
]


def easter_sunday(year):
    """Gregorian Easter Sunday (Meeus/Jones/Butcher algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def insert_event(conn, title, day, recurrence="none"):
    day_s = day.isoformat()
    conn.execute(
        '''INSERT INTO events
        (title, calendar, start_date, end_date, start_time, end_time, all_day, location, notes, recurrence)
        VALUES (?, 'spolecne', ?, ?, '', '', 1, '', ?, ?)''',
        (title, day_s, day_s, SYSTEM_NOTE, recurrence),
    )


def seed():
    app.init_db()
    with app.db() as conn:
        # Regenerate only our automatic holiday rows on every add-on start.
        conn.execute("DELETE FROM events WHERE notes = ?", (SYSTEM_NOTE,))

        # Fixed-date holidays can use the calendar's built-in yearly recurrence.
        for month, day, title in FIXED_HOLIDAYS:
            insert_event(
                conn,
                title,
                date(ANCHOR_YEAR, month, day),
                recurrence="yearly",
            )

        # Good Friday and Easter Monday move every year, so seed their actual dates.
        for year in range(EASTER_START_YEAR, EASTER_END_YEAR + 1):
            easter = easter_sunday(year)
            insert_event(conn, "🇨🇿 Velký pátek", easter - timedelta(days=2))
            insert_event(conn, "🇨🇿 Velikonoční pondělí", easter + timedelta(days=1))

        conn.commit()


if __name__ == "__main__":
    seed()
