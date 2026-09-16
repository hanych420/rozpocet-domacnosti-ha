"""Compatibility layer for Haneva shopping offers.

The original shopping_official module still owns SQLite schema, grouping,
filtering and shopping-list metadata. Version 0.10.x replaces the retired
HTML/PDF/Kupi acquisition path with external maintained data sources.
"""

import shopping_official_base as _base

# Re-export public and private helpers because consolidated_gateway.py uses a
# few internal grouping/serialization helpers from the original module.
for _name in dir(_base):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_base, _name)

import shopping_integrations_v100 as _sources_v100
import shopping_integrations_v102 as _sources
import shopping_translation_v104 as _translation_v104
import shopping_list_v104 as _list_v104

start_worker = _sources.start_worker
request_sync = _sources.request_sync
# shopping_list_v104 patches _base.enrich_state after the re-export above, so
# expose the patched function explicitly as well.
enrich_state = _list_v104.enrich_state
