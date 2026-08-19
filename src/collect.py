from __future__ import annotations
import json, math, os, re, time, random, hashlib, io
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urljoin
import requests
import pandas as pd
import yfinance as yf
import feedparser

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "public/data/copper_fundamentals.json"
HISTORY = ROOT / "public/data/copper_history.json"
UA = {"User-Agent":"Mozilla/5.0 Copper-Cycle-Engine/1.0", "Accept":"text/html,application/json"}
ENGINE_MODEL_VERSION = "COPPER_ENGINE_V3_20260819"

API_HEALTH = ROOT / "public/data/api_health.json"
try:
    PREVIOUS_PAYLOAD = json.loads(OUT.read_text()) if OUT.exists() else {}
except Exception:
    PREVIOUS_PAYLOAD = {}
try:
    PREVIOUS_HEALTH = json.loads(API_HEALTH.read_text()) if API_HEALTH.exists() else {}
except Exception:
    PREVIOUS_HEALTH = {}

SOURCE_HOMES = {
    "Yahoo Finance": "https://finance.yahoo.com/quote/HG=F/",
    "CFTC": "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm",
    "LME": "https://www.lme.com/en/market-data/reports-and-data/warehouse-and-stocks-reports",
    "SHFE": "https://www.shfe.com.cn/eng/reports/StatisticalData/WeeklyData/",
    "Google News RSS": "https://news.google.com/",
}

def iso_now():
    return datetime.now(timezone.utc).isoformat()

def parse_dt(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        # Some providers (notably yfinance/pandas conversions) can yield an
        # ISO timestamp without an explicit offset. Treat those timestamps as
        # UTC so age calculations never mix offset-naive and offset-aware
        # datetimes. Preserve aware timestamps and normalize them to UTC.
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        else:
            d = d.astimezone(timezone.utc)
        return d
    except Exception:
        return None

def age_seconds(v):
    d = parse_dt(v)
    return max(0.0, (datetime.now(timezone.utc) - d).total_seconds()) if d else None

def reliability_for(state, age_s=None, ttl_s=None, fallback_quality=0.8):
    if state == 'LIVE': return 1.0
    if state == 'CACHE': return 1.0 if age_s is None or ttl_s is None or age_s <= ttl_s else 0.95
    if state == 'FALLBACK': return max(0.35,min(0.95,float(fallback_quality)))
    if state == 'LKG':
        if age_s is None: return 0.55
        horizon=max(float(ttl_s or 86400),1.0)
        return max(0.20,0.90-0.65*min(age_s/horizon,1.5))
    return 0.0

class ApiRuntime:
    def __init__(self):
        self.memo={}
        self.stats={}
        self.last_call={}
        self.source_rows={}
    def _provider(self,url):
        host=urlparse(url).netloc.lower()
        if 'yahoo' in host: return 'Yahoo Finance'
        if 'cftc' in host: return 'CFTC'
        if 'lme.com' in host: return 'LME'
        if 'shfe.com' in host: return 'SHFE'
        if 'news.google.com' in host: return 'Google News RSS'
        return host or 'unknown'
    def _stat(self,p):
        return self.stats.setdefault(p,{'network_calls':0,'deduplicated':0,'cache_uses':0,'retries':0,'http_429':0,'timeouts':0,'errors':0})
    def _throttle(self,p):
        minimum={'Yahoo Finance':0.12,'CFTC':0.35,'LME':0.35,'SHFE':0.35,'Google News RSS':0.25}.get(p,0.12)
        last=self.last_call.get(p)
        if last is not None:
            wait=minimum-(time.monotonic()-last)
            if wait>0: time.sleep(wait)
        self.last_call[p]=time.monotonic()
    def request(self,url,*,params=None,headers=None,timeout=20,max_retries=2):
        p=self._provider(url); st=self._stat(p)
        key=(url,json.dumps(params or {},sort_keys=True,default=str))
        if key in self.memo:
            st['deduplicated']+=1
            return self.memo[key]
        last_exc=None
        for attempt in range(max_retries+1):
            self._throttle(p)
            try:
                st['network_calls']+=1
                r=requests.get(url,params=params,headers=headers or UA,timeout=timeout)
                if r.status_code==429:
                    st['http_429']+=1
                    if attempt<max_retries:
                        retry=r.headers.get('Retry-After')
                        try: delay=float(retry)
                        except Exception: delay=min(8.0,0.8*(2**attempt))+random.uniform(0.05,0.35)
                        st['retries']+=1; time.sleep(max(0.1,delay)); continue
                if 500<=r.status_code<600 and attempt<max_retries:
                    st['retries']+=1; time.sleep(min(6.0,0.6*(2**attempt))+random.uniform(0.05,0.35)); continue
                r.raise_for_status(); self.memo[key]=r; return r
            except requests.Timeout as e:
                st['timeouts']+=1; last_exc=e
            except Exception as e:
                last_exc=e
            if attempt<max_retries:
                st['retries']+=1; time.sleep(min(5.0,0.5*(2**attempt))+random.uniform(0.05,0.25))
        st['errors']+=1
        raise last_exc or RuntimeError('request failed')
    def mark(self,name,state,source,*,data_at=None,used=True,reliability=None,alternative=None,home=None,ttl_s=None):
        age=age_seconds(data_at)
        self.source_rows[name]={
          'status':state,'source':source,'dataAt':data_at,'ageSeconds':round(age,1) if age is not None else None,
          'usedInCalculation':bool(used),'reliability':round(float(reliability if reliability is not None else reliability_for(state,age,ttl_s)),3),
          'fallback':alternative,'url':home or SOURCE_HOMES.get(source), 'checkedAt':iso_now()
        }
    def health(self):
        return {'schemaVersion':'1.0','generatedAt':iso_now(),'sources':self.source_rows,'providers':self.stats}

RUNTIME=ApiRuntime()
_YF_MEMO={}

def previous_section(name):
    v=PREVIOUS_PAYLOAD.get(name)
    return json.loads(json.dumps(v)) if isinstance(v,dict) else None

def previous_checked(name):
    row=((PREVIOUS_HEALTH.get('sources') or {}).get(name) or {})
    return parse_dt(row.get('checkedAt'))

def is_due(name,hours):
    d=previous_checked(name)
    return d is None or (datetime.now(timezone.utc)-d).total_seconds() >= hours*3600

def lkg_or_unavailable(name, source, *, max_age_h=24, used=True):
    prev=previous_section(name)
    generated=PREVIOUS_PAYLOAD.get('generatedAt')
    age=age_seconds(generated)
    if prev is not None and (age is None or age <= max_age_h*3600):
        RUNTIME.mark(name,'LKG',source,data_at=generated,used=used,ttl_s=max_age_h*3600)
        return prev
    RUNTIME.mark(name,'UNAVAILABLE',source,data_at=generated,used=used,reliability=0.0)
    return None

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
    return RUNTIME.request(url, headers=UA, timeout=timeout)

def yahoo_history(symbol, period="10y", interval="1d"):
    key=(symbol,period,interval)
    if key in _YF_MEMO:
        RUNTIME._stat('Yahoo Finance')['deduplicated']+=1
        return _YF_MEMO[key]
    last_exc=None
    for attempt in range(3):
      try:
        RUNTIME._throttle('Yahoo Finance'); RUNTIME._stat('Yahoo Finance')['network_calls']+=1
        h=yf.Ticker(symbol).history(period=period,interval=interval,auto_adjust=False)
        if h is None or h.empty: raise RuntimeError('empty Yahoo history')
        out=[]
        for idx,row in h.iterrows():
            c=num(row.get("Close"));
            if c is None:continue
            out.append({"date":str(idx.date()),"close":c,"volume":num(row.get("Volume"))})
        _YF_MEMO[key]=out; return out
      except Exception as e:
        last_exc=e
        if attempt<2:
          RUNTIME._stat('Yahoo Finance')['retries']+=1; time.sleep(0.6*(2**attempt)+random.uniform(.05,.25))
    RUNTIME._stat('Yahoo Finance')['errors']+=1
    return []

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

def _contract_month_index(symbol):
    m=re.match(r'^HG([FGHJKMNQUVXZ])(\d{2})\.CMX$', str(symbol))
    if not m:return None
    month_map={'F':1,'G':2,'H':3,'J':4,'K':5,'M':6,'N':7,'Q':8,'U':9,'V':10,'X':11,'Z':12}
    return (2000+int(m.group(2)))*100+month_map[m.group(1)]

def futures_curve():
    codes="FGHJKMNQUVXZ"; now=datetime.now(timezone.utc); current_key=now.year*100+now.month; candidates=[]
    for y in (now.year,now.year+1):
      for code in codes:
        sym=contract_symbol(code,y); key=_contract_month_index(sym)
        if key is None or key<current_key: continue
        try:
          h=yf.Ticker(sym).history(period="10d",interval="1d",auto_adjust=False)
          if h is None or h.empty: continue
          row=h.iloc[-1]; price=num(row.get("Close")); vol=num(row.get("Volume")) or 0
          if price and price>0:candidates.append({"symbol":sym,"contractMonth":key,"price":price,"volume":vol})
        except Exception: pass
    candidates.sort(key=lambda x:x["contractMonth"])
    liquid=[x for x in candidates if x["volume"]>0] or candidates
    if len(liquid)<2:return {"status":"unavailable","source":"Yahoo individual COMEX contracts"}
    near=liquid[0]; far=liquid[min(3,len(liquid)-1)]
    spread=(far["price"]/near["price"]-1)*100; score=clamp(50+spread*25)
    return {"near":near,"far":far,"curveSpreadPct":spread,"curveScore":score,
            "structure":"contango" if spread>0.15 else "backwardation" if spread<-0.15 else "flat",
            "source":"Yahoo Finance individual COMEX copper contracts"}

def cftc_cot():
    select='report_date_as_yyyy_mm_dd,market_and_exchange_names,commodity_name,open_interest_all,m_money_positions_long_all,m_money_positions_short_all'
    urls=['https://publicreportinghub.cftc.gov/resource/72hh-3qpy.json','https://publicreporting.cftc.gov/resource/72hh-3qpy.json']
    rows=[];params={'$select':select,'$q':'COPPER','$order':'report_date_as_yyyy_mm_dd DESC','$limit':'500'}
    for url in urls:
      try:
        rows=RUNTIME.request(url,params=params,headers=UA,timeout=25).json()
        if rows:break
      except Exception:rows=[]
    by_date={}
    for r in rows:
      market=str(r.get('market_and_exchange_names') or '').upper(); commodity=str(r.get('commodity_name') or '').upper()
      if 'COPPER' not in market and 'COPPER' not in commodity:continue
      oi=num(r.get('open_interest_all'));lo=num(r.get('m_money_positions_long_all'));sh=num(r.get('m_money_positions_short_all'));dt=r.get('report_date_as_yyyy_mm_dd')
      if not dt or not oi or lo is None or sh is None:continue
      row={'date':dt,'oi':oi,'netPct':(lo-sh)/oi*100,'market':market}
      if dt not in by_date or oi>by_date[dt]['oi']:by_date[dt]=row
    vals=sorted(by_date.values(),key=lambda x:x['date'],reverse=True)
    if not vals:return {'status':'unavailable','source':'CFTC Public Reporting Hub'}
    latest=vals[0];prior=vals[1] if len(vals)>1 else latest
    oi_change=(latest['oi']/prior['oi']-1)*100 if prior.get('oi') else None
    return {'date':latest['date'],'openInterest':latest['oi'],'openInterestChangePct':oi_change,'netPct':latest['netPct'],
            'cotPercentile':pct_rank([x['netPct'] for x in vals],latest['netPct']),
            'netChangePp':latest['netPct']-prior['netPct'],
            'source':'CFTC Public Reporting Hub · Disaggregated Futures Only'}

def parse_first_number(text, patterns):
    for p in patterns:
      m=re.search(p,text,re.I|re.S)
      if m:
        v=num(m.group(1).replace(',',''))
        if v is not None:return v
    return None

def _numbers_from_row(row):
    vals=[]
    for x in list(row):
      if isinstance(x,str):x=x.replace(',','').strip()
      v=num(x)
      if v is not None:vals.append(v)
    return vals

def _copper_total_from_dataframe(df):
    if df is None or getattr(df,'empty',True):return None
    work=df.copy();cols=[str(c).strip().lower() for c in work.columns]
    for _,row in work.iterrows():
      text=' '.join(str(x) for x in row.tolist())
      if not re.search(r'\bcopper\b|铜|陰極銅|阴极铜',text,re.I):continue
      nums=[v for v in _numbers_from_row(row.tolist()) if 1000<=v<=5_000_000]
      if not nums:continue
      preferred=[]
      for idx,c in enumerate(cols):
        if idx>=len(row):continue
        if re.search(r'total|closing|close|stock|inventory|warehouse|库存|庫存',c,re.I):
          v=num(row.iloc[idx])
          if v is not None and 1000<=v<=5_000_000:preferred.append(v)
      return preferred[-1] if preferred else max(nums)
    return None

def _read_tables_from_response(resp):
    ctype=(resp.headers.get('content-type') or '').lower();url=str(getattr(resp,'url',''));out=[]
    try:
      if 'spreadsheet' in ctype or re.search(r'\.(xlsx?|xls)(?:\?|$)',url,re.I):
        out.extend(pd.read_excel(io.BytesIO(resp.content),sheet_name=None).values())
      else:
        out.extend(pd.read_html(io.StringIO(resp.text)))
    except Exception:pass
    return out

def _candidate_report_links(html,base_url,keywords):
    links=[]
    for href in re.findall(r'href=["\']([^"\']+)["\']',html or '',re.I):
      full=urljoin(base_url,href);low=full.lower()
      if re.search(r'\.(xlsx?|xls|csv)(?:\?|$)',low) and any(k in low for k in keywords):links.append(full)
    return list(dict.fromkeys(links))[:12]

def inventory_sources():
    out={"lmeInventoryTonnes":None,"shfeInventoryTonnes":None,"sources":{},"statuses":{},"dataAt":{}}
    lme_url='https://www.lme.com/en/market-data/reports-and-data/warehouse-and-stocks-reports'
    try:
      resp=fetch(lme_url);text=resp.text
      v=parse_first_number(text,[r'Copper.{0,1200}?Closing Stock[^0-9]{0,50}([0-9][0-9,]+)',r'Copper.{0,1200}?Total Stock[^0-9]{0,50}([0-9][0-9,]+)',r'Copper.{0,1200}?Opening Stock[^0-9]{0,50}([0-9][0-9,]+)'])
      if v and 1000<=v<=5_000_000:
        out['lmeInventoryTonnes']=v;out['sources']['lme']='LME warehouse and stocks public page'
      else:
        for link in _candidate_report_links(text,lme_url,['stock','warehouse','opening','closing','lme']):
          try:
            rr=fetch(link,timeout=30)
            for table in _read_tables_from_response(rr):
              found=_copper_total_from_dataframe(table)
              if found is not None:out['lmeInventoryTonnes']=found;out['sources']['lme']='LME official downloadable warehouse/stocks report';break
            if out['lmeInventoryTonnes'] is not None:break
          except Exception:pass
      if out['lmeInventoryTonnes'] is None:out['statuses']['lme']='official source reachable; machine-readable copper total not confirmed'
    except Exception as e:out['statuses']['lme']=f'fetch failed: {type(e).__name__}'
    for url in ['https://www.shfe.com.cn/eng/reports/StatisticalData/WeeklyData/','https://www.shfe.com.cn/reports/StatisticalData/WeeklyData/']:
      try:
        rr=fetch(url,timeout=30)
        for table in _read_tables_from_response(rr):
          found=_copper_total_from_dataframe(table)
          if found is not None:out['shfeInventoryTonnes']=found;out['sources']['shfe']='SHFE official weekly inventory table';break
        if out['shfeInventoryTonnes'] is None:
          for link in _candidate_report_links(rr.text,url,['week','inventory','stock','仓单','库存','weekly']):
            try:
              dl=fetch(link,timeout=30)
              for table in _read_tables_from_response(dl):
                found=_copper_total_from_dataframe(table)
                if found is not None:out['shfeInventoryTonnes']=found;out['sources']['shfe']='SHFE official weekly inventory download';break
              if out['shfeInventoryTonnes'] is not None:break
            except Exception:pass
        if out['shfeInventoryTonnes'] is not None:break
      except Exception:pass
    if out['shfeInventoryTonnes'] is None:out['statuses']['shfe']='official weekly inventory not machine-confirmed'
    today=date.today().isoformat()
    if out['lmeInventoryTonnes'] is not None:out['dataAt']['lme']=today
    if out['shfeInventoryTonnes'] is not None:out['dataAt']['shfe']=today
    return out

def china_cycle_proxy(copper_rows=None):
    fx=yahoo_history('FXI','1y','1d');copper=copper_rows or yahoo_history('HG=F','1y','1d')
    def mom(rows,n):return (rows[-1]['close']/rows[-1-n]['close']-1)*100 if len(rows)>n else None
    fxi20=mom(fx,20);cu20=mom(copper,20)
    if fxi20 is None:return {"chinaDemandProxyScore":None,"fxi20dPct":None,"copper20dPct":cu20,"manufacturingConstructionScore":None,"source":"Yahoo FXI 20-day market proxy","status":"unavailable"}
    score=clamp(50+3*fxi20)
    return {"chinaDemandProxyScore":score,"fxi20dPct":fxi20,"copper20dPct":cu20,"manufacturingConstructionScore":score,
            "source":"Yahoo FXI 20-day market proxy · copper price excluded from score"}

def concentrate_proxy(copper_rows=None):
    # TC/RC itself is paywalled. Use copper miners vs metal + curve as a free stress proxy.
    copx=yahoo_history('COPX','1y','1d'); cu=copper_rows or yahoo_history('HG=F','1y','1d')
    if len(copx)<21 or len(cu)<21:return {"status":"unavailable"}
    m=(copx[-1]['close']/copx[-21]['close']-1)*100-(cu[-1]['close']/cu[-21]['close']-1)*100
    # miners underperforming metal can indicate cost/supply stress; map to tightness then valuation offset.
    tight=clamp(50-m*3)
    return {"concentrateTightnessProxy":tight,"minersVsMetal20dPp":m,"source":"COPX vs HG=F relative-strength proxy"}

def disruptions():
    feeds=['https://news.google.com/rss/search?q=copper+mine+strike+OR+outage+OR+force+majeure+when:14d&hl=en-US&gl=US&ceid=US:en']
    keywords={'force majeure':18,'strike':12,'outage':12,'suspension':15,'accident':12,'guidance cut':12,'restart':-8,'recovery':-6}
    items=[];raw=0;seen=set()
    for url in feeds:
      d=feedparser.parse(fetch(url,timeout=20).content)
      for e in d.entries[:50]:
        title=(e.get('title') or '').strip();norm=re.sub(r'\s+',' ',title.lower());norm=re.sub(r'\s+-\s+[^-]{2,80}$','',norm)
        if not norm or norm in seen:continue
        seen.add(norm);hit=sum(w for k,w in keywords.items() if k in norm)
        if hit:
          hit=max(-12,min(24,hit));raw+=hit;items.append({"title":title,"link":e.get('link'),"impact":hit})
    return {"supplyDisruptionScore":clamp(raw,0,100),"events":items[:12],"eventCount":len(items),
            "source":"Google News RSS; deduplicated event score, not tonnage estimate"}

def clean_history_rows(data, today, new_row, retention_weeks=26):
    """Keep only valid, unique and recent history rows.

    Rules:
    - remove malformed rows
    - remove rows where official inventory and proxy score are all missing
    - replace an existing row for the same date
    - keep only the latest ``retention_weeks``
    - sort ascending by date
    """
    cutoff = date.fromisoformat(today) - timedelta(weeks=retention_weeks)
    rows_by_date = {}

    for row in data if isinstance(data, list) else []:
      if not isinstance(row, dict):
        continue
      row_date = row.get('date')
      if not row_date or row_date == today:
        continue
      try:
        parsed = date.fromisoformat(str(row_date))
      except Exception:
        continue
      if parsed < cutoff:
        continue
      has_value = (
        num(row.get('lme')) is not None or
        num(row.get('shfe')) is not None or
        num(row.get('inventoryScore')) is not None
      )
      if not has_value:
        continue
      rows_by_date[str(row_date)] = row

    if isinstance(new_row, dict):
      has_new_value = (
        num(new_row.get('lme')) is not None or
        num(new_row.get('shfe')) is not None or
        num(new_row.get('inventoryScore')) is not None
      )
      if has_new_value:
        rows_by_date[today] = new_row

    return [rows_by_date[k] for k in sorted(rows_by_date)]


def history_inventory(today, inv, proxy):
    try:data=json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    except Exception:data=[]
    lme=num(inv.get('lmeInventoryTonnes'));shfe=num(inv.get('shfeInventoryTonnes'));proxy_score=num(proxy.get('inventoryScore'))
    official_now=(lme is not None or shfe is not None)
    new_row={'date':today,'lme':lme,'shfe':shfe,'inventoryScore':proxy_score if not official_now else None,
             'inventoryMode':'official' if official_now else 'diagnostic_proxy'}
    data=clean_history_rows(data,today,new_row,retention_weeks=40)
    HISTORY.parent.mkdir(parents=True,exist_ok=True);HISTORY.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    official=[x for x in data if num(x.get('lme')) is not None or num(x.get('shfe')) is not None]
    def total(x):
      vals=[num(x.get('lme')),num(x.get('shfe'))];return sum(v for v in vals if v is not None)
    current=total(official[-1]) if official else None;prior=None
    if official:
      latest_date=date.fromisoformat(str(official[-1]['date']));target=latest_date-timedelta(days=91)
      eligible=[x for x in official[:-1] if date.fromisoformat(str(x['date']))<=target]
      if eligible:prior=total(eligible[-1])
    change=(current/prior-1)*100 if current and prior else None;vals=[total(x) for x in official if total(x)>0]
    return current,change,pct_rank(vals,current),len(official)

def free_inventory_proxy(curve, china, conc):
    """Fallback when official warehouse tonnage cannot be machine-read for free.
    This is explicitly a proxy score, not fabricated tonnes.
    Higher score means more apparent supply abundance / stronger overvaluation pressure.
    """
    curve_score=num(curve.get('curveScore'))
    demand=num(china.get('chinaDemandProxyScore'))
    tight=num(conc.get('concentrateTightnessProxy'))
    pieces=[]
    if curve_score is not None: pieces.append((curve_score,0.50))
    if demand is not None: pieces.append((100-demand,0.30))
    if tight is not None: pieces.append((100-tight,0.20))
    if not pieces:return {'inventoryScore':None,'inventoryChangePct13w':None,'source':'free proxy unavailable'}
    w=sum(x[1] for x in pieces)
    score=sum(v*wt for v,wt in pieces)/w
    # Directional equivalent only; deliberately labelled proxy in output/UI.
    trend=(score-50)*0.60
    return {'inventoryScore':clamp(score),'inventoryChangePct13w':trend,
            'source':'Free supply proxy: COMEX curve + China demand proxy + concentrate tightness proxy'}

def main():
    now=datetime.now(timezone.utc);today=now.date().isoformat()

    price=copper_price_block()
    if price.get('priceUsdPerLb') is None:
        price=lkg_or_unavailable('price','Yahoo Finance',max_age_h=8) or {"priceUsdPerLb":None,"longPricePercentile":None,"range1yPercentile":None,"ma20":None,"ma200":None,"momentum5dPct":None,"dailyCandles":[],"weeklyCandles":[],"source":"Yahoo Finance HG=F"}
    else:
        data_at=(price.get('dailyCandles') or [{}])[-1].get('date') if price.get('dailyCandles') else None
        RUNTIME.mark('price','LIVE','Yahoo Finance',data_at=data_at,home=SOURCE_HOMES['Yahoo Finance'])

    curve=futures_curve()
    if curve.get('curveSpreadPct') is None:
        curve=lkg_or_unavailable('curve','Yahoo Finance',max_age_h=8) or curve
    else: RUNTIME.mark('curve','LIVE','Yahoo Finance',data_at=now.isoformat())

    # Weekly/daily publications are not hammered every 30-minute market workflow.
    if is_due('cot',12):
        cot=cftc_cot()
        if cot.get('netPct') is None: cot=lkg_or_unavailable('cot','CFTC',max_age_h=10*24) or cot
        else: RUNTIME.mark('cot','LIVE','CFTC',data_at=cot.get('date'),ttl_s=10*24*3600)
    else:
        cot=previous_section('cot') or cftc_cot()
        RUNTIME.mark('cot','CACHE','CFTC',data_at=(cot or {}).get('date'),ttl_s=10*24*3600)

    if is_due('inventory',2):
        inv=inventory_sources()
        official_ok=inv.get('lmeInventoryTonnes') is not None or inv.get('shfeInventoryTonnes') is not None
        RUNTIME.mark('inventory','LIVE' if official_ok else 'FALLBACK','LME' if inv.get('lmeInventoryTonnes') is not None else 'SHFE' if inv.get('shfeInventoryTonnes') is not None else 'Yahoo Finance',data_at=today,reliability=1.0 if official_ok else 0.72,alternative=None if official_ok else 'Free supply proxy')
    else:
        prev_phys=previous_section('physical') or {}
        inv={k:prev_phys.get(k) for k in ('lmeInventoryTonnes','shfeInventoryTonnes','sources','statuses')}
        inv['sources']=inv.get('sources') or {}; inv['statuses']=inv.get('statuses') or {}
        RUNTIME.mark('inventory','CACHE','LME/SHFE',data_at=PREVIOUS_PAYLOAD.get('generatedAt'),ttl_s=24*3600)

    shared_copper=(price.get('dailyCandles') or [])
    china=china_cycle_proxy(shared_copper)
    if china.get('manufacturingConstructionScore') is None: china=lkg_or_unavailable('china','Yahoo Finance',max_age_h=8) or china
    else: RUNTIME.mark('china','LIVE','Yahoo Finance',data_at=now.isoformat())
    conc=concentrate_proxy(shared_copper)
    if conc.get('concentrateTightnessProxy') is None: conc=lkg_or_unavailable('concentrate','Yahoo Finance',max_age_h=8) or conc
    else: RUNTIME.mark('concentrate','LIVE','Yahoo Finance',data_at=now.isoformat())

    if is_due('supply',1):
        try: dis=disruptions()
        except Exception: dis=lkg_or_unavailable('supply','Google News RSS',max_age_h=24) or {"supplyDisruptionScore":None,"events":[],"source":"Google News RSS"}
        if dis.get('supplyDisruptionScore') is not None: RUNTIME.mark('supply','LIVE','Google News RSS',data_at=now.isoformat(),used=True,ttl_s=3600)
    else:
        dis=previous_section('supply') or disruptions(); RUNTIME.mark('supply','CACHE','Google News RSS',data_at=PREVIOUS_PAYLOAD.get('generatedAt'),ttl_s=3600)

    proxy=free_inventory_proxy(curve,china,conc)
    total,chg13,pct,official_obs=history_inventory(today,inv,proxy)
    inventory_mode='official' if total is not None else 'official_unavailable'
    physical={
      **inv,"visibleInventoryTonnes":total,"inventoryChangePct13w":chg13,"inventoryScore":pct,
      "inventoryMode":inventory_mode,"officialObservationCount":official_obs,
      "freeSupplyProxyScore":proxy.get('inventoryScore'),"freeSupplyProxyTrendEquivalent":proxy.get('inventoryChangePct13w'),
      "inventoryProxySource":proxy.get('source'),"chinaDemandProxyScore":china.get('chinaDemandProxyScore'),
      "curveSpreadPct":curve.get('curveSpreadPct'),"curveScore":curve.get('curveScore'),
      "concentrateTightnessProxy":conc.get('concentrateTightnessProxy')
    }
    if inventory_mode=='official':
        RUNTIME.mark('physical','LIVE','LME/SHFE',data_at=today,reliability=1.0)
    else:
        RUNTIME.mark('physical','UNAVAILABLE','LME/SHFE',data_at=today,reliability=0.0,alternative='diagnostic free supply proxy exists but is not official inventory')
        RUNTIME.mark('free_supply_proxy','FALLBACK','Yahoo Finance',data_at=now.isoformat(),used=False,reliability=0.70,alternative='COMEX curve + FXI China proxy + concentrate proxy')

    payload={"schemaVersion":"1.1","modelVersion":ENGINE_MODEL_VERSION,"generatedAt":now.isoformat(),"engine":"copper-cycle-engine","price":price,
             "physical":physical,"curve":curve,"cot":cot,"china":china,"concentrate":conc,"supply":dis,
             "apiHealth":RUNTIME.health(),
             "notes":["No paid data used","Official LME/SHFE inventory is never replaced by a proxy in official inventory fields",
                      "Free supply proxy is diagnostic-only when official inventory is unavailable",
                      "China cycle score uses FXI only; copper price momentum is diagnostic-only",
                      "13-week inventory change uses a calendar 91-day lag rather than observation count",
                      "Mine disruption score is deduplicated news-event evidence, not a tonnage estimate"]}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    API_HEALTH.write_text(json.dumps(RUNTIME.health(),ensure_ascii=False,indent=2))
    print(json.dumps({"ok":True,"out":str(OUT),"generatedAt":payload['generatedAt'],"apiHealth":str(API_HEALTH)},ensure_ascii=False))
if __name__=='__main__':main()
