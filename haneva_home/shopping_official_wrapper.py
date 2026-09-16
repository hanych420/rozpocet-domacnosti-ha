"""Compatibility layer for Haneva's retailer-specific leaflet synchronizer.

The original shopping_official module still owns the SQLite schema, grouping,
filtering and list metadata. Retailer-specific workers only swap the background
and manual sync entry points, so the rest of Haneva keeps the same API.
"""

import shopping_official_base as _base

# Re-export both public and private helpers because consolidated_gateway.py uses
# a few internal parsing/grouping helpers from the original module.
for _name in dir(_base):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_base, _name)

# Layer the retailer fixes so each version can reuse previous discovery, PDF
# extraction and source bookkeeping without duplicating the whole module.
import shopping_leaflets as _leaflets_v093
import shopping_leaflets_v094 as _leaflets_v094
import shopping_leaflets_v095 as _leaflets_v095
import shopping_leaflets_v096 as _leaflets_v096
import shopping_leaflets_v097 as _leaflets_v097
import shopping_leaflets_v098 as _leaflets

start_worker = _leaflets.start_worker
request_sync = _leaflets.request_sync
