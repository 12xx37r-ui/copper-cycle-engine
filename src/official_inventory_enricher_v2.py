"""Compatibility shim.

The inventory implementation is canonical in ``official_inventory_enricher``.
Keeping this module as a thin re-export prevents workflow/tests that still import
``official_inventory_enricher_v2`` from drifting onto a second copy of the parser.
"""
from official_inventory_enricher import *  # noqa: F401,F403
from official_inventory_enricher import run

if __name__ == "__main__":
    run()
