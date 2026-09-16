"""Compatibility layer for Haneva shopping offers.

The original shopping_official module still owns SQLite schema, grouping,
filtering and shopping-list metadata. Version 0.10.0 replaces the retired
HTML/PDF/Kupi acquisition path with external maintained data sources.
"""

import shopping_official_base as _base

# Re-export public and private helpers because consolidated_gateway.py uses a
# few internal grouping/serialization helpers from the original module.
for _name in dir(_base):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_base, _name)

import shopping_integrations_v100 as _sources

# The source module patches _base._sync_once and disables the Kupi fallback.
start_worker = _sources.start_worker
request_sync = _sources.request_sync
