"""Compatibility import shim for the canonical src inventory enricher.

This prevents an older repository-root copy from shadowing
`src/official_inventory_enricher.py` when the collector is launched from root.
"""
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parent
CANONICAL = ROOT / "src" / "official_inventory_enricher.py"
if not CANONICAL.exists():
    raise FileNotFoundError(f"canonical inventory enricher missing: {CANONICAL}")

_spec = importlib.util.spec_from_file_location("_copper_inventory_enricher_canonical", CANONICAL)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load canonical inventory enricher: {CANONICAL}")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

for _name in dir(_mod):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_mod, _name)

__file__ = str(CANONICAL)
