"""Compatibility entrypoint.

Keeps GitHub Actions safe whether the workflow runs `python collect.py`
or `python src/collect.py`. The canonical collector lives in src/collect.py.
"""
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent
CANONICAL = ROOT / "src" / "collect.py"
if not CANONICAL.exists():
    raise FileNotFoundError(f"canonical collector missing: {CANONICAL}")

if __name__ == "__main__":
    runpy.run_path(str(CANONICAL), run_name="__main__")
