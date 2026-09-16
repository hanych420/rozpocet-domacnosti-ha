from __future__ import annotations

"""Small deterministic Czech-name layer for Lidl API titles.

Lidl occasionally returns English product titles even for a Czech store. We do
not machine-translate arbitrary products; only known, unambiguous grocery names
are normalized so the shopping UI stays Czech.
"""

import shopping_integrations_v100 as source_base
import shopping_official_base as core

_TRANSLATIONS = {
    "red seedless table grapes": "Červené stolní hrozny bezsemenné",
    "green seedless table grapes": "Zelené stolní hrozny bezsemenné",
    "seedless table grapes": "Stolní hrozny bezsemenné",
    "iceberg lettuce": "Ledový salát",
    "cherry tomatoes": "Cherry rajčata",
    "blueberries": "Borůvky",
    "raspberries": "Maliny",
    "strawberries": "Jahody",
    "bananas": "Banány",
    "avocado": "Avokádo",
    "cucumber": "Okurka",
}

_original_sync_lidl = source_base._sync_lidl_from_ha


def _translate_saved_lidl_rows():
    changed = 0
    with core.db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM official_deals WHERE store='lidl' AND source_kind='official'"
        ).fetchall()
        for row in rows:
            translated = _TRANSLATIONS.get(core._norm(row["name"]))
            if not translated or translated == row["name"]:
                continue
            group_key, group_label = core._group_for(translated)
            conn.execute(
                "UPDATE official_deals SET name=?, name_norm=?, group_key=?, group_label=? WHERE id=?",
                (translated, core._norm(translated), group_key, group_label, row["id"]),
            )
            changed += 1
        conn.commit()
    if changed:
        source_base._log(f"Lidl CZ názvy: přeloženo {changed} známých anglických názvů")


def _sync_lidl_with_czech_names():
    count = _original_sync_lidl()
    _translate_saved_lidl_rows()
    return count


source_base._sync_lidl_from_ha = _sync_lidl_with_czech_names
