# Copper Engine V4.2 diagnostics patch

- Adds structured `physical.diagnostics.lme[]` and `physical.diagnostics.shfe[]`.
- Captures route, URL, exception class, HTTP status, content type, normalized reason and checkedAt.
- Does not persist response bodies, cookies, tokens or request headers.
- Records parser failures separately from transport failures.
- Captures deterministic monthly XLSX failures, HTML discovery failures, discovered XLSX failures and daily report failures.
- Workflow prints LME/SHFE diagnostics on every run.
- Engine model version bumped to `COPPER_ENGINE_V4_2_20260821`.
