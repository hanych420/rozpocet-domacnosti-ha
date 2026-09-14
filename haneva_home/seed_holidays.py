from datetime import date, timedelta

import app

MARKER_PREFIX = "__system_cz_holiday__"
ANCHOR_YEAR = 2026
EASTER_START_YEAR = 2020
EASTER_END_YEAR = 2100

FIXED_HOLIDAYS = [
    (1, 1, "🇨🇿 Nový rok / Den obnovy ČR", "new_year"),
    (5, 1, "🇨🇿 Svátek práce", "labour_day"),
    (5, 8, "🇨🇿 Den vítězství", "victory_day"),
    (7, 5, "🇨🇿 Cyril a Metoděj", "cyril_methodius"),
    (7, 6, "🇨🇿 Jan Hus", "jan_hus"),
    (9, 28, "🇨🇿 Den české státnosti", "statehood"),
    (10, 28, "🇨🇿 Den vzniku Československa", "czechoslovakia"),
    (11, 17, "🇨🇿 17. listopad", "november_17"),
    (12, 24, "🎄 Štědrý den", "christmas_eve"),
    (12, 25, "🎄 1. svátek vánoční", "christmas_day"),
    (12, 26, "🎄 2. svátek vánoční", "boxing_day"),
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


def insert_event(conn, title, day, marker, recurrence="none"):
    day_s = day.isoformat()
    conn.execute(
        '''INSERT INTO events
        (title, calendar, start_date, end_date, start_time, end_time, all_day, location, notes, recurrence)
        VALUES (?, 'spolecne', ?, ?, '', '', 1, '', ?, ?)''',
        (title, day_s, day_s, f"{MARKER_PREFIX}:{marker}", recurrence),
    )


def seed():
    app.init_db()
    with app.db() as conn:
        conn.execute("DELETE FROM events WHERE notes LIKE ?", (f"{MARKER_PREFIX}:%",))

        for month, day, title, key in FIXED_HOLIDAYS:
            insert_event(
                conn,
                title,
                date(ANCHOR_YEAR, month, day),
                key,
                recurrence="yearly",
            )

        for year in range(EASTER_START_YEAR, EASTER_END_YEAR + 1):
            easter = easter_sunday(year)
            insert_event(
                conn,
                "🇨🇿 Velký pátek",
                easter - timedelta(days=2),
                f"good_friday_{year}",
            )
            insert_event(
                conn,
                "🇨🇿 Velikonoční pondělí",
                easter + timedelta(days=1),
                f"easter_monday_{year}",
            )

        conn.commit()


if __name__ == "__main__":
    seed()
