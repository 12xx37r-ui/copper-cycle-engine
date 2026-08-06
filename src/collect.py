from __future__ import annotations
import json, math, os, re, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests
import pandas as pd
import yfinance as yf
import feedparser

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "public/data/copper_fundamentals.json"
HISTORY = ROOT / "public/data/copper_history.json"
UA = {"User-Agent":"Mozilla/5.0 Copper-Cycle-Engine/1.0", "Accept":"text/html,application/json"}

def num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except Exception:return None

def clamp(v,a=0,b=100): return max(a,min(b,v)) if v is not None else None

def pct_rank(values,current):
    vals=[float(x) for x in values if num(x) is not None]
    if not vals or current is None:return None
    return 100*sum(x<=current for x in vals)/len(vals)

def fetch(url, timeout=20):
    r=requests.get(url,headers=UA,timeout=timeout)
    r.raise_for_status(); return r

def yahoo_history(symbol, period="10y", interval="1d"):
    h=yf.Ticker(symbol).history(period=period,interval=interval,auto_adjust=False)
    if h is None or h.empty:return []
    out=[]
    for idx,row in h.iterrows():
        c=num(row.get("Close"));
        if c is None:continue
        out.append({"date":str(idx.date()),"close":c,"volume":num(row.get("Volume"))})
    return out

def copper_price_block():
    daily=yahoo_history("HG=F","10y","1d")
    weekly=yahoo_history("HG=F","10y","1wk")
    cur=daily[-1]["close"] if daily else None
    closes=[x["close"] for x in daily]
    one=closes[-252:]
    return {
      "priceUsdPerLb":cur,
      "longPricePercentile":pct_rank(closes,cur),
      "range1yPercentile":pct_rank(one,cur),
      "ma20":sum(closes[-20:])/20 if len(closes)>=20 else None,
      "ma200":sum(closes[-200:])/200 if len(closes)>=200 else None,
      "momentum5dPct":(cur/closes[-6]-1)*100 if len(closes)>5 and cur else None,
      "dailyCandles":daily[-220:],"weeklyCandles":weekly[-180:],
      "source":"Yahoo Finance HG=F"
    }

def contract_symbol(month_code, year): return f"HG{month_code}{str(year)[-2:]}.CMX"

def futures_curve():
    # COMEX standard delivery months plus nearby serial months. Select liquid contracts dynamically.
    codes="FGHJKMNQUVXZ"
    now=datetime.now(timezone.utc)
    candidates=[]
    for y in (now.year,now.year+1):
      for code in codes:
        sym=contract_symbol(code,y)
        try:
          h=yf.Ticker(sym).history(period="7d",interval="1d",auto_adjust=False)
          if h is None or h.empty: continue
          row=h.iloc[-1]; price=num(row.get("Close")); vol=num(row.get("Volume")) or 0
          if price and price>0: candidates.append({"symbol":sym,"price":price,"volume":vol})
        except Exception: pass
    candidates.sort(key=lambda x:(x["symbol"][-6:-4],x["symbol"][-4]))
    liquid=[x for x in candidates if x["volume"]>0] or candidates
    if len(liquid)<2:return {"status":"unavailable","source":"Yahoo individual COMEX contracts"}
    near=liquid[0]
    far=liquid[min(3,len(liquid)-1)]
    spread=(far["price"]/near["price"]-1)*100
    score=clamp(50+spread*25) # contango => higher overvaluation score
    return {"near":near,"far":far,"curveSpreadPct":spread,"curveScore":score,
            "structure":"contango" if spread>0.15 else "backwardation" if spread<-0.15 else "flat",
            "source":"Yahoo Finance individual COMEX copper contracts"}

def cftc_cot():
    select='report_date_as_yyyy_mm_dd,open_interest_all,m_money_positions_long_all,m_money_positions_short_all'
    market="COPPER-GRADE #1 - COMMODITY EXCHANGE INC."
    url='https://publicreporting.cftc.gov/resource/72hh-3qpy.json'
    params={'$select':select,'$where':f"market_and_exchange_names='{market}'",'$order':'report_date_as_yyyy_mm_dd DESC','$limit':'156'}
    try: rows=requests.get(url,params=params,headers=UA,timeout=20).json()
    except Exception:return {"status":"unavailable","source":"CFTC"}
    vals=[]
    for r in rows:
      oi=num(r.get('open_interest_all')); lo=num(r.get('m_money_positions_long_all')); sh=num(r.get('m_money_positions_short_all'))
      if oi and lo is not None and sh is not None: vals.append({"date":r.get('report_date_as_yyyy_mm_dd'),"oi":oi,"netPct":(lo-sh)/oi*100})
    if not vals:return {"status":"unavailable","source":"CFTC"}
    latest=vals[0]; prior=vals[1] if len(vals)>1 else latest
    return {"date":latest['date'],"openInterest":latest['oi'],"netPct":latest['netPct'],
            "cotPercentile":pct_rank([x['netPct'] for x in vals],latest['netPct']),
            "netChangePp":latest['netPct']-prior['netPct'],"source":"CFTC public Socrata API"}

def parse_first_number(text, patterns):
    for p in patterns:
      m=re.search(p,text,re.I|re.S)
      if m:
        v=num(m.group(1).replace(',',''))
        if v is not None:return v
    return None

def inventory_sources():
    out={"lmeInventoryTonnes":None,"shfeInventoryTonnes":None,"sources":{},"statuses":{}}
    # LME official downloadable page changes often: try page plus common embedded JSON patterns.
    try:
      text=fetch('https://www.lme.com/en/market-data/reports-and-data/warehouse-and-stocks-reports').text
      v=parse_first_number(text,[r'Copper.{0,500}?Opening Stock[^0-9]{0,30}([0-9,]+)',r'Copper.{0,500}?Closing Stock[^0-9]{0,30}([0-9,]+)'])
      if v: out['lmeInventoryTonnes']=v; out['sources']['lme']='LME warehouse and stocks public page'
      else: out['statuses']['lme']='official page reachable but machine-readable copper total not exposed'
    except Exception as e: out['statuses']['lme']=f'fetch failed: {type(e).__name__}'
    # SHFE official weekly inventory page/table. Try English and Chinese report pages.
    for url in ['https://www.shfe.com.cn/eng/reports/StatisticalData/WeeklyData/','https://www.shfe.com.cn/reports/StatisticalData/WeeklyData/']:
      try:
        tables=pd.read_html(fetch(url).text)
        found=None
        for t in tables:
          txt=' '.join(map(str,t.astype(str).values.flatten()))
          if re.search(r'Copper|铜',txt,re.I):
            nums=[num(x) for x in re.findall(r'\d[\d,]*',txt)]
            nums=[x for x in nums if x and x>1000]
            if nums: found=nums[-1]; break
        if found:
          out['shfeInventoryTonnes']=found;out['sources']['shfe']='SHFE official weekly inventory';break
      except Exception: pass
    if out['shfeInventoryTonnes'] is None:out['statuses']['shfe']='official table format unavailable; proxy used'
    return out

def china_cycle_proxy():
    # Free and durable market proxy: China ETF + copper/China relative momentum. Official PMI can be supplied later.
    fx=yahoo_history('FXI','1y','1d'); copper=yahoo_history('HG=F','1y','1d')
    def mom(rows,n):
      return (rows[-1]['close']/rows[-1-n]['close']-1)*100 if len(rows)>n else None
    fxi20=mom(fx,20); cu20=mom(copper,20)
    score=clamp(50+(fxi20 or 0)*2+(cu20 or 0))
    return {"chinaDemandProxyScore":score,"fxi20dPct":fxi20,"copper20dPct":cu20,
            "manufacturingConstructionScore":score,"source":"Yahoo FXI + HG=F free market proxy"}

def concentrate_proxy():
    # TC/RC itself is paywalled. Use copper miners vs metal + curve as a free stress proxy.
    copx=yahoo_history('COPX','1y','1d'); cu=yahoo_history('HG=F','1y','1d')
    if len(copx)<21 or len(cu)<21:return {"status":"unavailable"}
    m=(copx[-1]['close']/copx[-21]['close']-1)*100-(cu[-1]['close']/cu[-21]['close']-1)*100
    # miners underperforming metal can indicate cost/supply stress; map to tightness then valuation offset.
    tight=clamp(50-m*3)
    return {"concentrateTightnessProxy":tight,"minersVsMetal20dPp":m,"source":"COPX vs HG=F relative-strength proxy"}

def disruptions():
    feeds=['https://news.google.com/rss/search?q=copper+mine+strike+OR+outage+OR+force+majeure+when:14d&hl=en-US&gl=US&ceid=US:en']
    keywords={'force majeure':18,'strike':12,'outage':12,'suspension':15,'accident':12,'guidance cut':12,'restart':-8,'recovery':-6}
    items=[]; raw=0
    for url in feeds:
      d=feedparser.parse(url)
      for e in d.entries[:40]:
        title=e.get('title','').lower(); hit=sum(w for k,w in keywords.items() if k in title)
        if hit: raw+=hit;items.append({"title":e.get('title'),"link":e.get('link'),"impact":hit})
    return {"supplyDisruptionScore":clamp(raw,0,100),"events":items[:12],"source":"Google News RSS; event score, not tonnage estimate"}

def history_inventory(today, inv):
    try:data=json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    except Exception:data=[]
    row={"date":today,"lme":inv.get('lmeInventoryTonnes'),"shfe":inv.get('shfeInventoryTonnes')}
    data=[x for x in data if x.get('date')!=today]+[row];data=data[-520:]
    HISTORY.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    valid=[x for x in data if num(x.get('lme')) is not None or num(x.get('shfe')) is not None]
    def total(x):return sum(v for v in [num(x.get('lme')),num(x.get('shfe'))] if v is not None)
    current=total(valid[-1]) if valid else None
    prior=total(valid[-14]) if len(valid)>=14 else None
    change=(current/prior-1)*100 if current and prior else None
    vals=[total(x) for x in valid if total(x)>0]
    return current,change,pct_rank(vals,current)

def main():
    now=datetime.now(timezone.utc);today=now.date().isoformat()
    price=copper_price_block();curve=futures_curve();cot=cftc_cot();inv=inventory_sources();china=china_cycle_proxy();conc=concentrate_proxy();dis=disruptions()
    total,chg13,pct=history_inventory(today,inv)
    # If official inventory unavailable, do not fabricate it. Score is omitted and coverage honestly falls.
    inventory_score=pct
    physical={
      **inv,"visibleInventoryTonnes":total,"inventoryChangePct13w":chg13,"inventoryScore":inventory_score,
      "chinaDemandProxyScore":china.get('chinaDemandProxyScore'),"curveSpreadPct":curve.get('curveSpreadPct'),
      "curveScore":curve.get('curveScore'),"concentrateTightnessProxy":conc.get('concentrateTightnessProxy')
    }
    payload={"schemaVersion":"1.0","generatedAt":now.isoformat(),"engine":"copper-cycle-engine","price":price,
             "physical":physical,"curve":curve,"cot":cot,"china":china,"concentrate":conc,"supply":dis,
             "notes":["No paid data used","Missing official values are omitted, never fabricated","China spot premium and TC/RC are represented by clearly-labelled free proxies"]}
    OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    print(json.dumps({"ok":True,"out":str(OUT),"generatedAt":payload['generatedAt']},ensure_ascii=False))
if __name__=='__main__':main()
