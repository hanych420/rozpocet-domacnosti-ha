"""Compatibility layer for Haneva's retailer-specific leaflet synchronizer.

The original shopping_official module still owns the SQLite schema, grouping,
filtering and list metadata. 0.9.3 only swaps its background/request sync entry
points for the stronger Lidl/Albert leaflet worker, so the rest of the app does
not need to know about the implementation change.
"""

import shopping_official_base as _base

# Re-export both public and private helpers because consolidated_gateway.py uses
# a few internal parsing/grouping helpers from the original module.
for _name in dir(_base):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_base, _name)

import shopping_leaflets as _leaflets

start_worker = _leaflets.start_worker
request_sync = _leaflets.request_sync
