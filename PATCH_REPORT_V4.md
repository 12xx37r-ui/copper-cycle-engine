# Copper Engine / Radar freshness patch V4 — 2026-08-21

## Engine
- COMEX curve now uses retry/throttle/provider health accounting instead of bare `yf.Ticker(...).history()` with swallowed exceptions.
- COMEX output now carries provider `dataAt`, engine `checkedAt`, per-leg timestamps, bounded error diagnostics, and explicit tenor metadata.
- COMEX far leg is selected by target ~3-month tenor rather than array position.
- `apiHealth.sources.curve.dataAt` now reflects the Yahoo observation, not engine generation time.
- Added canonical `inventoryEvidence` for downstream consumers: usable/status/evidence class/provider/dataAt/checkedAt/official-direct status/fallback reason.
- LME request failures now retain route + exception + HTTP code where available.
- `official_inventory_enricher_v2.py` is now a compatibility shim over the canonical enricher to remove duplicated logic.

## GAS / Radar
- Copper Engine transport state is separated from metric/provider state.
- A failed GitHub refresh now produces bounded LKG transport semantics instead of falsely marking every Copper metric `REFRESH_FAILED`.
- LME mirror is represented as `FALLBACK`/2nd-party data, while official-direct LME failure remains separate metadata.
- COMEX curve status/dataAt/checkedAt come from Copper Engine metric health.
- Copper current-position, short curve, and long inventory metrics receive their actual observation/check timestamps.
- Whole-radar snapshot refresh failure preserves provider `FALLBACK` and maps old LIVE/CACHE metrics to `LKG`; it records `transportStatus=REFRESH_FAILED` separately.

## UI
- `REFRESH_FAILED` label now means provider/source refresh failure, not generic transport failure.
- LKG caused by a failed refresh gets an amber warning rather than a red provider-failure warning.
- FALLBACK gets an explicit "official/primary route replaced by declared alternate source" notice.
- Westmetall is recognized as its own route/source in freshness metadata.
