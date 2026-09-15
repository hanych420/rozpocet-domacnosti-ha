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

# Load the 0.9.3 implementation first; 0.9.4 builds on its PDF extraction and
# source bookkeeping but replaces discovery with retailer-specific strategies.
import shopping_leaflets as _leaflets_v093
import shopping_leaflets_v094 as _leaflets

start_worker = _leaflets.start_worker
request_sync = _leaflets.request_sync
