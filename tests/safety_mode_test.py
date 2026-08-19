from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("collect", ROOT / "src" / "collect.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

def rows_from_return(ret_pct, n=30):
    """
    Create n+1 closes whose 20-day return is exactly ret_pct.
    china_cycle_proxy() compares rows[-1] with rows[-21].
    """
    base = 100.0
    rows = [{"close": base, "date": str(i)} for i in range(n + 1)]
    rows[-21]["close"] = base
    rows[-1]["close"] = base * (1.0 + ret_pct / 100.0)
    return rows

orig_yahoo_history = m.yahoo_history

try:
    # ------------------------------------------------------------------
    # V3 contract:
    # China-cycle score is FXI-only.
    # Copper momentum is diagnostic and MUST NOT alter the score.
    # FXI +4% => 50 + 3*4 = 62.
    # ------------------------------------------------------------------
    def both_live(symbol, period="1y", interval="1d"):
        if symbol == "FXI":
            return rows_from_return(4.0)
        if symbol == "HG=F":
            return rows_from_return(2.0)
        return []

    m.yahoo_history = both_live
    r = m.china_cycle_proxy(rows_from_return(2.0))
    expected = 62.0
    assert abs(r["chinaDemandProxyScore"] - expected) < 1e-9, (r, expected)
    assert abs(r["manufacturingConstructionScore"] - expected) < 1e-9, r
    assert abs(r["fxi20dPct"] - 4.0) < 1e-9, r
    assert abs(r["copper20dPct"] - 2.0) < 1e-9, r
    assert "copper price excluded from score" in r["source"].lower(), r

    # Copper price direction must not change the China-cycle score.
    r_up = m.china_cycle_proxy(rows_from_return(25.0))
    r_down = m.china_cycle_proxy(rows_from_return(-25.0))
    assert abs(r_up["chinaDemandProxyScore"] - expected) < 1e-9, r_up
    assert abs(r_down["chinaDemandProxyScore"] - expected) < 1e-9, r_down

    # ------------------------------------------------------------------
    # FXI missing:
    # Do NOT silently fall back to copper price because that reintroduces
    # the self-reference V3 intentionally removed.
    # ------------------------------------------------------------------
    def fxi_missing(symbol, period="1y", interval="1d"):
        if symbol == "FXI":
            return []
        if symbol == "HG=F":
            return rows_from_return(8.0)
        return []

    m.yahoo_history = fxi_missing
    r = m.china_cycle_proxy(rows_from_return(8.0))
    assert r["chinaDemandProxyScore"] is None, r
    assert r["manufacturingConstructionScore"] is None, r
    assert r.get("status") == "unavailable", r
    assert r["copper20dPct"] is not None, r

    # ------------------------------------------------------------------
    # FXI live, copper unavailable:
    # Score remains valid because copper is diagnostic-only.
    # ------------------------------------------------------------------
    def copper_missing(symbol, period="1y", interval="1d"):
        if symbol == "FXI":
            return rows_from_return(-3.0)
        if symbol == "HG=F":
            return []
        return []

    m.yahoo_history = copper_missing
    r = m.china_cycle_proxy([])
    expected = 41.0  # 50 + 3*(-3)
    assert abs(r["chinaDemandProxyScore"] - expected) < 1e-9, (r, expected)
    assert abs(r["manufacturingConstructionScore"] - expected) < 1e-9, r
    assert r["copper20dPct"] is None, r

finally:
    m.yahoo_history = orig_yahoo_history

print("safety mode ok · V3 FXI-only China score")
