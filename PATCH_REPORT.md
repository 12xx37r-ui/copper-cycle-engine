# API Safety Patch Report

## Scope reviewed
- Current deployed monolithic `Code.gs` supplied in this conversation: all `UrlFetchApp.fetch/fetchAll`, `safeJson_`, `safeText_`, GitHub Raw/Contents/CDN paths, ResearchBitcoin/BTCFunk/BGeometrics, derivatives venues, Yahoo, FRED, CFTC, WGC, metal/public pages and related fallbacks.
- Current `Index.html` UI/refresh logic and static theme/company JS.
- `btc-cycle-radar-main.zip` source snapshot and tests.
- `copper-cycle-engine-main.zip`: `src/collect.py`, output JSON, tests and GitHub Actions workflow.

## Important project finding
The supplied `btc-cycle-radar-main.zip` is an older/minimal source snapshot and is not equivalent to the supplied current `Code.gs`. Its GAS collector only has a small set of CoinGecko/Upbit/Gold-API/Frankfurter/FRED calls, while the current `Code.gs` contains the much larger active production routing. I did not overwrite that older source tree with the monolith because that would hide the mismatch. The deploy-ready patch is in `radar-patched/`.

## Main changes
1. All direct Apps Script external fetch paths now route through guarded network wrappers with same-run request memoization, provider throttling, bounded retries, `Retry-After` handling for 429, exponential backoff+jitter and per-provider counters.
2. Existing per-URL cache/stale logic is reused. State metadata is now explicit: `LIVE / CACHE / LKG / FALLBACK / UNAVAILABLE` with source, age, reliability, error/fallback metadata.
3. Radar envelope cache was reduced from 10 minutes to 2 minutes so fresh market data can appear sooner. Per-source adaptive TTLs still prevent a full external re-fetch every 2 minutes.
4. Whole-radar stale/persistent snapshot returns are explicitly marked `LKG` rather than looking live.
5. USD/KRW no longer falls through to the hard-coded `1400` placeholder. It uses Frankfurter -> ER-API fallback -> bounded LKG -> unavailable. KRW conversions are guarded so missing FX is not coerced into zero.
6. A-engine item LKG is now age-bounded. Existing normal outputs/formulas are unchanged; old persistent LKG cannot survive indefinitely.
7. Copper GitHub engine JSON is checked by its own `generatedAt`, not merely by whether GitHub returned HTTP 200. On weekdays output older than 2h is marked LKG and older than 12h is excluded; weekend allowance is wider (12h / 48h).
8. Bottom API safety panel is collapsed by default. It shows source/status/actual source/data age/calculation use/reliability/fallback, severe stale/unavailable warning, provider call counters, and clickable source/API links.
9. Copper GitHub workflow changed from once daily to every 30 minutes on weekdays, with a 6-hour weekend health refresh. Market sources are queried every market workflow; slow publication sources use release-frequency-aware reuse.
10. Copper engine now writes `public/data/api_health.json` and embeds `apiHealth` in its JSON output. Provider network calls, deduplication, retries, 429, timeouts and errors are recorded.
11. Copper `china_cycle_proxy()` no longer substitutes a missing input with numeric `0`. With both inputs present, the original formula is exactly preserved. In degraded states only, available inputs are renormalized; if none exist the score is `None`.

## Regression / safety tests
- Existing BTC repository tests: PASS (`npm test`).
- Current `Code.gs` syntax: PASS (`node --check` after `.js` copy).
- Current `Index.html` script syntax: PASS.
- Apps Script safety mock suite: PASS 11/11, including live, same-run dedupe, cache, LKG, LKG expiry, individual source failure isolation, independent FX fallback, multiple simultaneous failures, all-core failure and null-not-zero behavior.
- Copper safety tests: PASS. Normal two-leg China formula equals previous formula exactly; degraded single-leg is renormalized; zero-input is unavailable.
- Existing copper output validation: PASS.
- Normal score engine regression: the complete scoring-engine region before the embedded HTML marker is byte-for-byte identical between original and patched `Code.gs`.

## Files modified
### Radar
- `Code.gs`
- `Index.html`
- Added `EXTERNAL_DATA_AUDIT.md`
- Added `API_SOURCE_INVENTORY.json`
- Added `tests/api_safety.test.js`

### Copper engine
- `src/collect.py`
- `.github/workflows/update-copper.yml`
- `public/data/copper_fundamentals.json` (additive `apiHealth` only for seeded health metadata)
- Added `public/data/api_health.json`
- Added `tests/safety_mode_test.py`

## Not modified because source was not supplied
The A-engine repositories referenced by current Code.gs (`global-macro-data-collector`, `fed-futures-collector`) were not present in the uploaded files, so their internal Python/Actions cannot be patched here. Their read-side transport, cache/LKG age handling and failure behavior in the radar were hardened. The supplied Copper GitHub engine was patched end-to-end, including workflow scheduling and `api_health.json`.
