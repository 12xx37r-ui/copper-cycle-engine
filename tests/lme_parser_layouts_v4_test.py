from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from official_inventory_enricher import _copper_total_from_table, _table_probe, _lme_stocks_summary_total_from_table, _lme_queue_table_has_stock_tonnage_context

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


def test_lme_stocks_summary_ca_code_layout():
    import pandas as pd
    df = pd.DataFrame([
        [None, None, "BusinessDate", "AA", "AH", "CA", "CO", None, "NI", "PB", "SN", "ZS"],
        [None, None, "2026-07-29", 1000, 2000, 228500, 10, None, 3000, 4000, 5000, 6000],
        [None, None, "2026-07-30", 1000, 2000, 229750, 10, None, 3000, 4000, 5000, 6000],
        [None, None, "2026-07-31", 1000, 2000, 231125, 10, None, 3000, 4000, 5000, 6000],
    ])
    assert _lme_stocks_summary_total_from_table(df) == 231125


def test_lme_queue_waiting_days_never_treated_as_inventory():
    import pandas as pd
    df = pd.DataFrame([
        [" ", None, "Key: / metal not listed", "Waiting time in days: April 2026", None],
        ["Country/Region", "Location", "Warehouse Company", "Aluminium Alloy", "Copper"],
        ["Belgium", "Antwerp", "Warehouse A", 0, 0],
    ])
    assert _lme_queue_table_has_stock_tonnage_context(df) is False
