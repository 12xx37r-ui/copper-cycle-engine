from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from official_inventory_enricher import _copper_total_from_table, _table_probe

# Row-oriented report
df = pd.DataFrame([["Metal", "Closing stock"], ["Copper", 235975], ["Zinc", 100000]])
assert _copper_total_from_table(df) == 235975

# Column-oriented time series
df = pd.DataFrame([
    ["Date", "Copper", "Zinc"],
    ["2026-07-30", 240000, 90000],
    ["2026-07-31", 235975, 91000],
])
assert _copper_total_from_table(df) == 235975

# Copper worksheet with total row and no Copper body label
df = pd.DataFrame([["Warehouse stock report", None], ["Closing Stock Total", 235975]])
df.attrs["sheet_name"] = "Copper"
assert _copper_total_from_table(df) == 235975

probe = _table_probe(df)
assert probe["sheet"] == "Copper"
assert probe["rows"] == 2 and probe["cols"] == 2
assert probe["sample"]
print("lme parser layouts v4 ok")
