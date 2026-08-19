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

# Supply-disruption false-positive guard:
# unrelated real-estate force-majeure headlines must never score.
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime

class _E:
    def __init__(self,title,published):
        self._d={'title':title,'link':'https://example.com/x','published':published}
    def get(self,k,default=None):
        return self._d.get(k,default)

class _Feed:
    def __init__(self,entries):
        self.entries=entries

orig_parse=m.feedparser.parse
orig_fetch=m.fetch
try:
    fresh=format_datetime(datetime.now(timezone.utc)-timedelta(days=1))
    m.fetch=lambda *a,**k: type("Resp",(),{"content":b""})()
    m.feedparser.parse=lambda *a,**k:_Feed([
        _E("MahaRERA extends project completion deadlines citing force majeure",fresh),
        _E("Codelco copper mine strike forces suspension at El Teniente",fresh)
    ])
    s=m.disruptions()
    assert s["eventCount"]==1, s
    assert s["supplyDisruptionScore"]>0, s
    assert "Codelco" in s["events"][0]["title"], s
    assert s["undatedRejected"]==0, s
finally:
    m.feedparser.parse=orig_parse
    m.fetch=orig_fetch

print("safety mode ok · V3 FXI-only + copper supply relevance")


# V3 supply filter regression: ambiguous "Freeport LNG" must be rejected and
# duplicate articles for the same copper asset must collapse to one event.
class _E2:
    def __init__(self,title,published):
        self._d={'title':title,'link':'https://example.com/'+str(abs(hash(title))),
                 'published':published}
    def get(self,k,default=None):
        return self._d.get(k,default)

class _Feed2:
    def __init__(self,entries):
        self.entries=entries

orig_parse2=m.feedparser.parse
orig_fetch2=m.fetch
try:
    fresh2=format_datetime(datetime.now(timezone.utc)-timedelta(days=1))
    m.fetch=lambda *a,**k:type("Resp",(),{"content":b""})()
    m.feedparser.parse=lambda *a,**k:_Feed2([
        _E2("Freeport LNG Restarts Texas Export Plant After Power-Related Shutdown",fresh2),
        _E2("Cobre Panama restart nears as workers return to copper mine",fresh2),
        _E2("First Quantum adds jobs as Cobre Panama restart nears",fresh2),
        _E2("Freeport Indonesia pushes back Grasberg copper restart by a year",fresh2)
    ])
    s2=m.disruptions()
    titles=' | '.join(x['title'] for x in s2['events']).lower()
    assert 'freeport lng' not in titles, s2
    assert s2['eventCount']==2, s2
    assert s2['filterVersion']==m.SUPPLY_FILTER_VERSION, s2
finally:
    m.feedparser.parse=orig_parse2
    m.fetch=orig_fetch2

print("safety mode ok · V3 supply asset clustering")


# V4 supply-date regression: stale articles must be rejected even if Google RSS
# returns them for a `when:14d` query.
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime

class _E3:
    def __init__(self,title,published):
        self._d={'title':title,'link':'https://example.com/'+str(abs(hash(title))),
                 'published':published}
    def get(self,k,default=None):
        return self._d.get(k,default)

class _Feed3:
    def __init__(self,entries):
        self.entries=entries

orig_parse3=m.feedparser.parse
orig_fetch3=m.fetch
try:
    now=datetime.now(timezone.utc)
    fresh=format_datetime(now-timedelta(days=1))
    stale=format_datetime(now-timedelta(days=100))
    m.fetch=lambda *a,**k:type("Resp",(),{"content":b""})()
    m.feedparser.parse=lambda *a,**k:_Feed3([
        _E3("Codelco halts El Teniente copper mine over strike",fresh),
        _E3("Codelco halts Chuquicamata copper mine over strike",stale)
    ])
    s3=m.disruptions()
    titles=' | '.join(x['title'] for x in s3['events']).lower()
    assert 'el teniente' in titles, s3
    assert 'chuquicamata' not in titles, s3
    assert s3['staleRejected']>=1, s3
    assert s3['windowDays']==14, s3
finally:
    m.feedparser.parse=orig_parse3
    m.fetch=orig_fetch3

print("safety mode ok · V4 strict 14-day supply window")
