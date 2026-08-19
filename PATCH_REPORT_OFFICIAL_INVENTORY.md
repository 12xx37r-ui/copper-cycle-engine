# Copper official-inventory enrichment patch

## Goal
Recover the four dashboard-facing outputs without fabricating official data:

1. official visible-inventory percentile
2. official inventory 13-week change
3. official visible-inventory 13-week trend score
4. mine supply-disruption score (preserved from the existing V4 supply engine)

## Design
- Existing `src/collect.py` remains untouched in this patch.
- A post-collector `src/official_inventory_enricher.py` reads the normal engine JSON, collects/maintains official inventory history, computes derived metrics, and writes them back.
- LME monthly Stocks Summary is the primary historical route.
- LME Warehouse Company Stocks and Queue report is a second official LME monthly route.
- SHFE weekly inventory remains an official current route when machine-readable.
- CME/COMEX `Copper_Stocks.xls` is added as a separately labelled official basket. It is never silently mixed with LME/SHFE because report-unit semantics differ.
- 13-week change is calendar 91-day lag, same-basket only.
- Percentile requires at least 12 official observations.
- Historical backfill never pretends to be today's observation: `dataAt` is the report date.
- Missing official data remains null/UNAVAILABLE.

## Important dependency fix
The repository previously used `pandas.read_excel()` for XLS/XLSX but did not pin the engines required by pandas. This patch adds:
- `openpyxl==3.1.5` for XLSX
- `xlrd==2.0.1` for legacy XLS

This matters for both LME XLSX and CME/LME legacy XLS reports.

## New engine JSON block
`officialIndicators` exposes the four dashboard outputs directly.

## Tests
`tests/official_inventory_test.py` covers:
- calendar 91-day lag
- percentile minimum history
- falling-inventory trend direction
- basket mismatch rejection
- historical-backfill date honesty
- LME-only and SHFE-only operation
- no proxy/fabricated inventory when unavailable
- mine-supply score passthrough including a valid zero-event score
