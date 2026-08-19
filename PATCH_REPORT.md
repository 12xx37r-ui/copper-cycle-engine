# Copper inventory force-fill V3

Purpose: populate the three missing inventory-derived dashboard indicators whenever direct official exchange downloads are blocked, without fabricating data.

Priority:
1. LME / SHFE / CME official exchange reports
2. Westmetall daily LME Copper stock mirror as explicit `exchange_mirror` FALLBACK
3. null / UNAVAILABLE if neither path is usable

The Westmetall parser is intentionally independent of pandas column-header inference. It parses every table row by date + cash + 3-month + stock structure, with HTML-row and flattened-text fallbacks. The collector itself invokes the enricher, so the workflow cannot publish a base JSON before enrichment.

Mirror semantics remain explicit:
- basket: `lme_mirror`
- inventoryMode: `exchange_mirror_lme`
- inventoryEvidenceClass: `exchange_mirror`
- officialSourceAvailable: false
- apiHealth status: FALLBACK

The dashboard-compatible fields are populated from the mirror only when the mirror has a recent observation and enough same-basket history. 13-week change uses calendar 91-day lag; percentile needs at least 12 observations. No price/curve/proxy value is inserted into these inventory fields.
