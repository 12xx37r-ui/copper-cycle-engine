import importlib.util, math, sys, types
sys.modules.setdefault('yfinance', types.SimpleNamespace(Ticker=lambda *a,**k: None))
sys.modules.setdefault('feedparser', types.SimpleNamespace(parse=lambda *a,**k: types.SimpleNamespace(entries=[])))
from pathlib import Path
P=Path(__file__).resolve().parents[1]/'src'/'collect.py'
spec=importlib.util.spec_from_file_location('collect',P); c=importlib.util.module_from_spec(spec); spec.loader.exec_module(c)

# LIVE/CACHE/LKG/FALLBACK/UNAVAILABLE reliability semantics
assert c.reliability_for('LIVE') == 1.0
assert c.reliability_for('CACHE',10,60) >= .95
assert 0 < c.reliability_for('LKG',3600,7200) < 1
assert c.reliability_for('FALLBACK',fallback_quality=.72) == .72
assert c.reliability_for('UNAVAILABLE') == 0

# Normal-state regression: new renormalizable China formula exactly equals old formula when both legs exist.
def rows(ret):
    a=[{'date':str(i),'close':100.0,'volume':1} for i in range(25)]
    a[-21]['close']=100.0; a[-1]['close']=100.0*(1+ret/100)
    return a
orig_yh=c.yahoo_history
try:
    c.yahoo_history=lambda symbol,period='1y',interval='1d': rows(4.0) if symbol=='FXI' else rows(2.0)
    r=c.china_cycle_proxy(rows(2.0))
    old=50+4.0*2+2.0
    assert abs(r['chinaDemandProxyScore']-old)<1e-9, (r,old)
    # One missing leg: no fake zero/neutral placeholder; remaining signal is renormalized.
    c.yahoo_history=lambda symbol,period='1y',interval='1d': [] if symbol=='FXI' else rows(2.0)
    r2=c.china_cycle_proxy(rows(2.0))
    assert r2['chinaDemandProxyScore'] is not None and r2['chinaDemandProxyScore'] != 50
    # Both missing => unavailable, never 50.
    r3=c.china_cycle_proxy([])
    # shared empty triggers internal HG fetch; force both empty
    c.yahoo_history=lambda *a,**k: []
    r3=c.china_cycle_proxy([])
    assert r3['chinaDemandProxyScore'] is None
finally:
    c.yahoo_history=orig_yh

# Inventory fallback already renormalizes only available pieces; no input => None.
assert c.free_inventory_proxy({}, {}, {})['inventoryScore'] is None
assert c.free_inventory_proxy({'curveScore':60},{},{})['inventoryScore'] == 60

print('copper safety tests ok')
