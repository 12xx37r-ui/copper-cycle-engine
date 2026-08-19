import sys, types, json, tempfile, pathlib, importlib.util
from datetime import date, timedelta

# Stub optional network packages so pure-logic tests run without installing project dependencies.
if 'yfinance' not in sys.modules:
    yf=types.ModuleType('yfinance'); yf.Ticker=lambda *a,**k: None; sys.modules['yfinance']=yf
if 'feedparser' not in sys.modules:
    fp=types.ModuleType('feedparser'); fp.parse=lambda *a,**k: types.SimpleNamespace(entries=[]); sys.modules['feedparser']=fp

ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('collect',ROOT/'src'/'collect.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

assert m._contract_month_index('HGU26.CMX')==202609
assert m._contract_month_index('HGZ26.CMX')==202612

orig=m.yahoo_history
def fake(symbol,period='1y',interval='1d'):
    if symbol=='FXI': return [{'close':100+i*.1,'date':str(i)} for i in range(30)]
    return [{'close':100*(1.2**i),'date':str(i)} for i in range(30)]
m.yahoo_history=fake
a=m.china_cycle_proxy([{'close':100*(1.2**i),'date':str(i)} for i in range(30)])
b=m.china_cycle_proxy([{'close':100*(0.8**i),'date':str(i)} for i in range(30)])
assert abs(a['manufacturingConstructionScore']-b['manufacturingConstructionScore'])<1e-12
m.yahoo_history=orig

old_hist=m.HISTORY
with tempfile.TemporaryDirectory() as td:
    m.HISTORY=pathlib.Path(td)/'h.json'
    start=date(2026,1,1);rows=[]
    for k in range(0,120,7):
        d=start+timedelta(days=k);rows.append({'date':d.isoformat(),'lme':100000+k*100,'shfe':50000,'inventoryMode':'official'})
    m.HISTORY.write_text(json.dumps(rows))
    total,chg,pct,n=m.history_inventory((start+timedelta(days=119)).isoformat(),
        {'lmeInventoryTonnes':112000,'shfeInventoryTonnes':50000},{'inventoryScore':88})
    assert total==162000 and chg is not None and n>=10
m.HISTORY=old_hist
print('4 passed')
