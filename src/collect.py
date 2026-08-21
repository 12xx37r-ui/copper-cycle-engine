from __future__ import annotations
import json, math, os, re, time, random, hashlib, io
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
UA = {
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":"en-US,en;q=0.9"
}
ENGINE_MODEL_VERSION = "COPPER_ENGINE_V4_7_20260821"
SUPPLY_FILTER_VERSION = "COPPER_SUPPLY_FILTER_V4_20260819"
INVENTORY_COLLECTOR_VERSION = "COPPER_INVENTORY_V5_20260821"

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

def _request_headers(url):
    h=dict(UA)
    low=str(url).lower()
    if re.search(r'\.(xlsx?|xls)(?:\?|$)',low):
        h["Accept"]="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,application/octet-stream,*/*;q=0.8"
    if 'lme.com' in low:
        h["Referer"]="https://www.lme.com/Market-data/Reports-and-data/Warehouse-and-stocks-reports/Stocks-summary"
        h["Sec-Fetch-Site"]="same-origin"
        h["Sec-Fetch-Mode"]="navigate"
    elif 'shfe.com.cn' in low:
        h["Referer"]="https://www.shfe.com.cn/eng/reports/index.html"
        h["Accept-Language"]="en-US,en;q=0.9,zh-CN;q=0.7"
    return h

def fetch(url, timeout=20):
    return RUNTIME.request(url, headers=_request_headers(url), timeout=timeout)

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

def _yf_contract_snapshot(symbol: str, period: str = "10d") -> tuple[dict[str, Any] | None, str | None]:
    """Fetch one COMEX contract with the same retry/throttle/health accounting as other Yahoo routes.

    Returns (snapshot, error).  The snapshot carries the provider observation date,
    which is deliberately distinct from the engine check/generation time.
    """
    last_exc = None
    for attempt in range(3):
        try:
            RUNTIME._throttle('Yahoo Finance')
            RUNTIME._stat('Yahoo Finance')['network_calls'] += 1
            h = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
            if h is None or h.empty:
                raise RuntimeError('empty Yahoo contract history')
            row = h.iloc[-1]
            price = num(row.get("Close"))
            if price is None or price <= 0:
                raise RuntimeError('invalid Yahoo contract close')
            vol = num(row.get("Volume")) or 0
            idx = h.index[-1]
            try:
                data_at = idx.to_pydatetime()
                if data_at.tzinfo is None:
                    data_at = data_at.replace(tzinfo=timezone.utc)
                else:
                    data_at = data_at.astimezone(timezone.utc)
                data_at = data_at.isoformat()
            except Exception:
                data_at = str(getattr(idx, 'date', lambda: idx)())
            return {"symbol": symbol, "price": price, "volume": vol, "dataAt": data_at}, None
        except Exception as e:
            last_exc = e
            if attempt < 2:
                RUNTIME._stat('Yahoo Finance')['retries'] += 1
                time.sleep(0.6 * (2 ** attempt) + random.uniform(.05, .25))
    RUNTIME._stat('Yahoo Finance')['errors'] += 1
    return None, f"{type(last_exc).__name__}: {last_exc}" if last_exc else 'unknown Yahoo error'


def _month_distance(a: int, b: int) -> int:
    ay, am = divmod(int(a), 100); by, bm = divmod(int(b), 100)
    return (by - ay) * 12 + (bm - am)


def futures_curve():
    codes = "FGHJKMNQUVXZ"
    now = datetime.now(timezone.utc)
    current_key = now.year * 100 + now.month
    candidates = []
    errors = []
    for y in (now.year, now.year + 1):
        for code in codes:
            sym = contract_symbol(code, y)
            key = _contract_month_index(sym)
            if key is None or key < current_key:
                continue
            snap, err = _yf_contract_snapshot(sym)
            if snap is None:
                errors.append({"symbol": sym, "error": err})
                continue
            snap["contractMonth"] = key
            candidates.append(snap)

    candidates.sort(key=lambda x: x["contractMonth"])
    liquid = [x for x in candidates if x["volume"] > 0] or candidates
    if len(liquid) < 2:
        return {
            "status": "UNAVAILABLE",
            "source": "Yahoo Finance individual COMEX copper contracts",
            "errors": errors[-8:],
            "checkedAt": iso_now(),
        }

    near = liquid[0]
    # Use a stable ~3-month tenor rather than an array position that can change
    # when one intermediate contract has zero volume or temporarily disappears.
    target_months = 3
    farther = liquid[1:]
    far = min(farther, key=lambda x: (abs(_month_distance(near["contractMonth"], x["contractMonth"]) - target_months), x["contractMonth"]))
    tenor_months = _month_distance(near["contractMonth"], far["contractMonth"])
    spread = (far["price"] / near["price"] - 1) * 100
    score = clamp(50 + spread * 25)
    # The spread is only as fresh as its older leg.
    observed = [parse_dt(near.get("dataAt")), parse_dt(far.get("dataAt"))]
    observed = [x for x in observed if x is not None]
    data_at = min(observed).isoformat() if observed else None
    return {
        "near": near,
        "far": far,
        "tenorMonths": tenor_months,
        "targetTenorMonths": target_months,
        "curveSpreadPct": spread,
        "curveScore": score,
        "structure": "contango" if spread > 0.15 else "backwardation" if spread < -0.15 else "flat",
        "source": "Yahoo Finance individual COMEX copper contracts",
        "status": "LIVE",
        "dataAt": data_at,
        "checkedAt": iso_now(),
        "errors": errors[-8:],
    }

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


def _month_end(year, month):
    if month == 12:
        nxt=date(year+1,1,1)
    else:
        nxt=date(year,month+1,1)
    return nxt-timedelta(days=1)

def _extract_lme_monthly_links(html, base_url):
    """Extract public LME Stocks summary Excel links with their month/year labels."""
    months={m.lower():i for i,m in enumerate(
        ['January','February','March','April','May','June','July','August','September','October','November','December'],1)}
    found=[]
    # Capture anchors whose visible text contains "Stocks <Month> <Year>".
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or '', re.I|re.S):
        href=m.group(1); label=re.sub(r'<[^>]+>',' ',m.group(2)); label=re.sub(r'\s+',' ',label).strip()
        lm=re.search(r'\bStocks\s+([A-Za-z]+)\s+(20\d{2})\b',label,re.I)
        if not lm: continue
        mon=months.get(lm.group(1).lower())
        if not mon: continue
        found.append((int(lm.group(2)),mon,urljoin(base_url,href)))
    # newest first, unique by year/month
    out=[]; seen=set()
    for y,mo,u in sorted(found,key=lambda x:(x[0],x[1]),reverse=True):
        if (y,mo) in seen: continue
        seen.add((y,mo)); out.append((y,mo,u))
    return out

def _lme_monthly_stock_history(max_reports=5, diagnostics=None):
    """
    Seed official LME history from the public monthly Stocks summary XLSX files.
    Important: use deterministic official file URLs first, because the LME HTML
    report page can return WAF/HTTP errors to GitHub Actions even while the
    public XLSX files remain directly addressable.
    """
    month_names=['january','february','march','april','may','june',
                 'july','august','september','october','november','december']
    now=date.today()
    rows=[]
    diagnostics = diagnostics if isinstance(diagnostics, list) else []
    # Start from previous month: monthly stock summary is published in arrears.
    y,m=now.year,now.month-1
    if m==0:
        y-=1;m=12
    attempts=0
    while attempts < max_reports + 4 and len(rows) < max_reports:
        name=month_names[m-1]
        url=(f'https://www.lme.com/-/media/files/data/reports-and-data/'
             f'warehouse-and-stock-reports/stocks-summary/stocks-{name}-{y}.xlsx')
        try:
            rr=fetch(url,timeout=30)
            found=None
            for table in _read_tables_from_response(rr):
                found=_copper_total_from_dataframe(table)
                if found is not None:
                    break
            if found is not None:
                rows.append({
                    'date':_month_end(y,m).isoformat(),
                    'lme':found,'shfe':None,
                    'inventoryScore':None,'inventoryMode':'official_monthly',
                    'source':'LME Stocks summary',
                    'sourceUrl':url
                })
            else:
                diagnostics.append(_parse_failure_record('LME monthly deterministic XLSX', url, rr))
        except Exception as e:
            diagnostics.append(_request_error_record(e, 'LME monthly deterministic XLSX', url))
        m-=1
        if m==0:
            y-=1;m=12
        attempts+=1

    # HTML discovery is a secondary fallback only.
    if len(rows) < min(4,max_reports):
        url='https://www.lme.com/en/Market-data/Reports-and-data/Warehouse-and-stocks-reports/Stocks-summary'
        try:
            resp=fetch(url,timeout=30)
            links=_extract_lme_monthly_links(resp.text,url)
            known={x['date'] for x in rows}
            for y,mo,link in links:
                d=_month_end(y,mo).isoformat()
                if d in known:
                    continue
                try:
                    rr=fetch(link,timeout=30)
                    found=None
                    for table in _read_tables_from_response(rr):
                        found=_copper_total_from_dataframe(table)
                        if found is not None:
                            break
                    if found is not None:
                        rows.append({
                            'date':d,'lme':found,'shfe':None,
                            'inventoryScore':None,'inventoryMode':'official_monthly',
                            'source':'LME Stocks summary','sourceUrl':link
                        })
                        known.add(d)
                        if len(rows)>=max_reports:
                            break
                    else:
                        diagnostics.append(_parse_failure_record('LME monthly discovered XLSX', link, rr))
                except Exception as e:
                    diagnostics.append(_request_error_record(e, 'LME monthly discovered XLSX', link))
        except Exception as e:
            diagnostics.append(_request_error_record(e, 'LME monthly stocks-summary HTML discovery', url))

    return sorted(rows,key=lambda x:x['date'])

def _merge_official_backfill(history_rows, backfill_rows):
    by_date={}
    for row in history_rows if isinstance(history_rows,list) else []:
        if isinstance(row,dict) and row.get('date'):
            by_date[str(row['date'])]=row
    for row in backfill_rows if isinstance(backfill_rows,list) else []:
        if not isinstance(row,dict) or not row.get('date'): continue
        old=by_date.get(str(row['date'])) or {}
        # Never overwrite a same-date official component with None.
        merged=dict(old)
        for k,v in row.items():
            if v is not None or k not in merged:
                merged[k]=v
        by_date[str(row['date'])]=merged
    return [by_date[k] for k in sorted(by_date)]

def StringStatus_(v: Any) -> str:
    return str(v or '').strip()


def _request_error_record(exc: Exception, route: str, url: str) -> dict:
    """Structured provider diagnostics without persisting bodies, cookies, or secrets."""
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None) if response is not None else None
    headers = getattr(response, 'headers', {}) or {} if response is not None else {}
    ctype = headers.get('content-type') if hasattr(headers, 'get') else None
    kind = type(exc).__name__
    reason = None
    if status in (401, 403):
        reason = 'authentication_or_waf_restriction'
    elif status == 429:
        reason = 'rate_limited'
    elif status is not None and int(status) >= 500:
        reason = 'upstream_server_error'
    elif kind in ('Timeout', 'ReadTimeout', 'ConnectTimeout'):
        reason = 'timeout'
    elif kind == 'HTTPError':
        reason = 'http_error'
    else:
        reason = 'request_error'
    return {
        'route': route,
        'url': url,
        'exception': kind,
        'statusCode': status,
        'contentType': ctype,
        'reason': reason,
        'checkedAt': datetime.now(timezone.utc).isoformat()
    }

def _parse_failure_record(route: str, url: str, response=None, reason='copper_inventory_not_found') -> dict:
    headers = getattr(response, 'headers', {}) or {} if response is not None else {}
    ctype = headers.get('content-type') if hasattr(headers, 'get') else None
    return {
        'route': route,
        'url': url,
        'exception': 'ParseFailure',
        'statusCode': getattr(response, 'status_code', None) if response is not None else None,
        'contentType': ctype,
        'reason': reason,
        'checkedAt': datetime.now(timezone.utc).isoformat()
    }

def _request_error_detail(exc: Exception, route: str, url: str) -> str:
    """Backward-compatible compact rendering of the structured diagnostic."""
    rec = _request_error_record(exc, route, url)
    parts = [route, rec['exception']]
    if rec.get('statusCode') is not None:
        parts.append(f"HTTP {rec['statusCode']}")
    if rec.get('reason'):
        parts.append(rec['reason'])
    parts.append(url)
    return ' · '.join(parts)


def inventory_sources():
    out={
      "lmeInventoryTonnes":None,"shfeInventoryTonnes":None,
      "sources":{},"statuses":{},"dataAt":{},"cadence":{},
      "diagnostics":{"lme":[],"shfe":[]}
    }

    # 1) LME current/daily public warehouse page route.
    lme_url='https://www.lme.com/en/market-data/reports-and-data/warehouse-and-stocks-reports'
    try:
      resp=fetch(lme_url); text=resp.text
      v=parse_first_number(text,[
        r'Copper.{0,1200}?Closing Stock[^0-9]{0,50}([0-9][0-9,]+)',
        r'Copper.{0,1200}?Total Stock[^0-9]{0,50}([0-9][0-9,]+)',
        r'Copper.{0,1200}?Opening Stock[^0-9]{0,50}([0-9][0-9,]+)'
      ])
      if v and 1000<=v<=5_000_000:
        out['lmeInventoryTonnes']=v
        out['sources']['lme']='LME warehouse and stocks public page'
        out['dataAt']['lme']=date.today().isoformat()
        out['cadence']['lme']='daily'
      else:
        for link in _candidate_report_links(text,lme_url,['stock','warehouse','opening','closing','lme']):
          try:
            rr=fetch(link,timeout=30)
            for table in _read_tables_from_response(rr):
              found=_copper_total_from_dataframe(table)
              if found is not None:
                out['lmeInventoryTonnes']=found
                out['sources']['lme']='LME official downloadable warehouse/stocks report'
                out['dataAt']['lme']=date.today().isoformat()
                out['cadence']['lme']='daily'
                break
            if out['lmeInventoryTonnes'] is not None: break
          except Exception as e:
            out['diagnostics']['lme'].append(_request_error_record(e, 'LME daily downloadable report', link))
        if out['lmeInventoryTonnes'] is None:
          out['diagnostics']['lme'].append(_parse_failure_record('LME daily warehouse page parse', lme_url, resp))
    except Exception as e:
      rec=_request_error_record(e, 'LME daily warehouse route', lme_url)
      out['diagnostics']['lme'].append(rec)
      out['statuses']['lme_daily']=_request_error_detail(e, 'LME daily warehouse route', lme_url)

    # 2) Official public LME monthly Stocks Summary fallback.
    # This is still official inventory; it is just lower cadence than the daily report.
    if out['lmeInventoryTonnes'] is None:
      monthly=_lme_monthly_stock_history(max_reports=5, diagnostics=out['diagnostics']['lme'])
      if monthly:
        latest=monthly[-1]
        out['lmeInventoryTonnes']=num(latest.get('lme'))
        out['sources']['lme']='LME Stocks summary (official monthly closing stock)'
        out['dataAt']['lme']=latest.get('date')
        out['cadence']['lme']='monthly'
        out['lmeMonthlyHistory']=monthly
      else:
        out['statuses']['lme']='official daily and monthly LME stock routes unavailable · '+StringStatus_(out['statuses'].get('lme_daily'))

    # 3) SHFE official weekly inventory.
    for url in [
      'https://www.shfe.com.cn/eng/reports/StatisticalData/WeeklyData/',
      'https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/?query_options=1&query_params=inventory'
    ]:
      try:
        rr=fetch(url,timeout=30)
        # Reject obvious WAF/challenge pages before table parsing.
        body=(rr.text or '')[:8000].lower()
        if 'web 应用防火墙' in body or '人机识别' in body or ('slide' in body and 'captcha' in body):
          out['diagnostics']['shfe'].append(_parse_failure_record('SHFE weekly inventory', url, rr, 'anti_bot_challenge'))
          continue
        for table in _read_tables_from_response(rr):
          found=_copper_total_from_dataframe(table)
          if found is not None:
            out['shfeInventoryTonnes']=found
            out['sources']['shfe']='SHFE official weekly inventory'
            out['dataAt']['shfe']=date.today().isoformat()
            out['cadence']['shfe']='weekly'
            break
        if out['shfeInventoryTonnes'] is not None: break
      except Exception as e:
        out['diagnostics']['shfe'].append(_request_error_record(e, 'SHFE weekly inventory', url))

    if out['shfeInventoryTonnes'] is None:
      out['statuses']['shfe']='official weekly inventory unavailable or blocked by SHFE anti-bot/WAF'

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
    """
    Copper-specific supply-event score with hard 14-day publication gate.
    Google News `when:14d` is not trusted by itself because RSS can surface
    stale/evergreen results. Articles without a parseable publication date are
    excluded from scoring.
    """
    now=datetime.now(timezone.utc)
    cutoff=now-timedelta(days=14)
    future_limit=now+timedelta(days=1)

    feeds=[
      'https://news.google.com/rss/search?q=%28%22copper+mine%22+OR+%22copper+smelter%22+OR+%22copper+refinery%22+OR+codelco+OR+escondida+OR+grasberg+OR+%22cobre+panama%22+OR+kamoa+OR+kakula+OR+collahuasi+OR+%22las+bambas%22+OR+%22cerro+verde%22+OR+quellaveco+OR+chuquicamata+OR+%22el+teniente%22+OR+viscaria%29+%28strike+OR+outage+OR+suspension+OR+%22force+majeure%22+OR+accident+OR+restart+OR+shutdown+OR+closure%29+when%3A14d&hl=en-US&gl=US&ceid=US%3Aen'
    ]

    event_keywords=[
      ('force majeure',18),('shutdown',15),('suspension',15),('closure',15),
      ('production cut',12),('guidance cut',12),('strike',12),('outage',12),
      ('accident',10),('fatal',10),('dies',8),
      ('restart delayed',10),('restart push',10),('pushes back',10),
      ('postponed',10),('delay',8),
      ('restart',-8),('recovery',-6),('resume',-6),('reopen',-6)
    ]

    assets={
      'cobre_panama':['cobre panama'],
      'grasberg':['grasberg','freeport indonesia'],
      'escondida':['escondida'],
      'el_teniente':['el teniente'],
      'chuquicamata':['chuquicamata'],
      'kamoa_kakula':['kamoa','kakula'],
      'collahuasi':['collahuasi'],
      'las_bambas':['las bambas'],
      'cerro_verde':['cerro verde'],
      'quellaveco':['quellaveco'],
      'viscaria':['viscaria'],
      'codelco_other':['codelco']
    }

    def event_time(entry):
        raw=entry.get('published') or entry.get('updated')
        if not raw:
            return None
        try:
            dt=parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt=dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            try:
                dt=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
                if dt.tzinfo is None:
                    dt=dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                return None

    candidates=[]
    stale_rejected=0
    undated_rejected=0

    for url in feeds:
      d=feedparser.parse(fetch(url,timeout=20).content)
      for e in d.entries[:100]:
        published_at=event_time(e)
        if published_at is None:
          undated_rejected+=1
          continue
        if published_at < cutoff or published_at > future_limit:
          stale_rejected+=1
          continue

        title=(e.get('title') or '').strip()
        norm=re.sub(r'\s+',' ',title.lower())
        norm=re.sub(r'\s+-\s+[^-]{2,80}$','',norm)
        if not norm:
          continue

        # Reject ambiguous non-copper company/context collisions.
        if any(x in norm for x in [
          'freeport lng','natural gas','realty','real estate','maharera','credai',
          'oil refinery','lng plant','gas plant'
        ]):
          continue

        asset=None
        for key,aliases in assets.items():
          if any(a in norm for a in aliases):
            asset=key
            break

        generic_copper=(
          'copper mine' in norm or 'copper mining' in norm or
          'copper smelter' in norm or 'copper refinery' in norm or
          ('copper' in norm and any(x in norm for x in [
            'mine','mining','smelter','refinery','concentrate'
          ]))
        )
        if asset is None and not generic_copper:
          continue

        impacts=[w for k,w in event_keywords if k in norm]
        if not impacts:
          continue

        delayed=any(x in norm for x in [
          'restart delayed','restart push','pushes back',
          'restart by a year','restart postponed','postponed'
        ])
        if delayed:
          hit=max([x for x in impacts if x>0] or [8])
        else:
          hit=max(impacts,key=lambda x:abs(x))
        hit=max(-12,min(20,hit))

        # Age decay: very recent events matter more; older within-window stories
        # remain evidence but cannot dominate.
        age_days=max(0.0,(now-published_at).total_seconds()/86400.0)
        age_factor=1.0 if age_days<=3 else 0.75 if age_days<=7 else 0.50
        weighted=int(round(hit*age_factor))

        if asset is None:
          fp=re.sub(r'[^a-z0-9 ]',' ',norm)
          fp=' '.join(w for w in fp.split() if w not in {
            'copper','mine','mining','the','a','an','in','at','on','of','to',
            'and','says','company'
          })
          asset='generic:'+(' '.join(fp.split()[:6]) or norm[:60])

        candidates.append({
          "asset":asset,
          "title":title,
          "link":e.get('link'),
          "impact":weighted,
          "rawImpact":hit,
          "ageDays":round(age_days,2),
          "published":published_at.isoformat()
        })

    # One contribution per copper asset. Prefer the newest event first; if two
    # events are on the same day, retain the stronger absolute impact.
    by_asset={}
    for x in sorted(candidates,key=lambda z:z['published'],reverse=True):
      old=by_asset.get(x['asset'])
      if old is None:
        by_asset[x['asset']]=x
      else:
        same_day=x['published'][:10]==old['published'][:10]
        if same_day and abs(x['impact'])>abs(old['impact']):
          by_asset[x['asset']]=x

    items=list(by_asset.values())
    raw=sum(x['impact'] for x in items)
    score=clamp(raw,0,100)
    items.sort(key=lambda x:x['published'],reverse=True)
    public_items=[{k:v for k,v in x.items() if k!='asset'} for x in items[:12]]

    return {
      "supplyDisruptionScore":score,
      "events":public_items,
      "eventCount":len(items),
      "rawNetImpact":raw,
      "staleRejected":stale_rejected,
      "undatedRejected":undated_rejected,
      "windowDays":14,
      "filterVersion":SUPPLY_FILTER_VERSION,
      "source":"Google News RSS; strict 14-day publication gate + copper-asset clustering + age decay; not tonnage estimate"
    }

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
    try:
        data=json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    except Exception:
        data=[]

    # Seed missing official history from the public LME monthly Stocks summary.
    # This avoids waiting 13 weeks before the 13-week inventory trend can exist.
    official_existing=[
      x for x in data if isinstance(x,dict) and
      (num(x.get('lme')) is not None or num(x.get('shfe')) is not None)
    ]
    if len(official_existing) < 4:
        # Backfill network access belongs exclusively to inventory_sources(),
        # which is cadence-gated. history_inventory() must remain pure/local;
        # otherwise a blocked LME endpoint gets hammered every workflow run.
        seeded=(inv.get('lmeMonthlyHistory') or []) if isinstance(inv,dict) else []
        if seeded:
            data=_merge_official_backfill(data,seeded)

    lme=num(inv.get('lmeInventoryTonnes'))
    shfe=num(inv.get('shfeInventoryTonnes'))
    proxy_score=num(proxy.get('inventoryScore'))
    official_now=(lme is not None or shfe is not None)

    new_row={
      'date':today,'lme':lme,'shfe':shfe,
      'inventoryScore':proxy_score if not official_now else None,
      'inventoryMode':'official' if official_now else 'diagnostic_proxy'
    }

    data=clean_history_rows(data,today,new_row,retention_weeks=60)
    HISTORY.parent.mkdir(parents=True,exist_ok=True)
    HISTORY.write_text(json.dumps(data,ensure_ascii=False,indent=2))

    official=[
      x for x in data if num(x.get('lme')) is not None or num(x.get('shfe')) is not None
    ]

    def total(x):
      vals=[num(x.get('lme')),num(x.get('shfe'))]
      return sum(v for v in vals if v is not None)

    current=total(official[-1]) if official else None
    prior=None
    if official:
      latest_date=date.fromisoformat(str(official[-1]['date']))
      target=latest_date-timedelta(days=91)
      eligible=[x for x in official[:-1] if date.fromisoformat(str(x['date']))<=target]
      if eligible:
        # nearest observation on/before 91-day target
        prior=total(eligible[-1])

    change=(current/prior-1)*100 if current and prior else None
    vals=[total(x) for x in official if total(x)>0]
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
    else:
        RUNTIME.mark('curve','LIVE','Yahoo Finance',data_at=curve.get('dataAt'),ttl_s=48*3600)
        # Keep collection/check timestamps in the metric payload as well as apiHealth.
        curve['checkedAt']=curve.get('checkedAt') or iso_now()

    # Weekly/daily publications are not hammered every 30-minute market workflow.
    if is_due('cot',12):
        cot=cftc_cot()
        if cot.get('netPct') is None: cot=lkg_or_unavailable('cot','CFTC',max_age_h=10*24) or cot
        else: RUNTIME.mark('cot','LIVE','CFTC',data_at=cot.get('date'),ttl_s=10*24*3600)
    else:
        cot=previous_section('cot') or cftc_cot()
        RUNTIME.mark('cot','CACHE','CFTC',data_at=(cot or {}).get('date'),ttl_s=10*24*3600)

    prev_phys=previous_section('physical') or {}
    prev_official=prev_phys.get('lmeInventoryTonnes') is not None or prev_phys.get('shfeInventoryTonnes') is not None
    # Exchange WAF/login restrictions are persistent, so retry on the normal
    # publication-aware cadence rather than hammering LME/SHFE every 30 minutes.
    if is_due('inventory',6):
        inv=inventory_sources()
        inv['collectorVersion']=INVENTORY_COLLECTOR_VERSION
        official_ok=inv.get('lmeInventoryTonnes') is not None or inv.get('shfeInventoryTonnes') is not None
        if official_ok:
            src='LME' if inv.get('lmeInventoryTonnes') is not None and inv.get('shfeInventoryTonnes') is None else 'SHFE' if inv.get('shfeInventoryTonnes') is not None and inv.get('lmeInventoryTonnes') is None else 'LME/SHFE'
            dates=[v for v in (inv.get('dataAt') or {}).values() if v]
            actual_at=max(dates) if dates else today
            state='LIVE' if actual_at==today else 'CACHE'
            rel=1.0 if state=='LIVE' else 0.9
            RUNTIME.mark('inventory',state,src,data_at=actual_at,used=True,reliability=rel,ttl_s=45*86400)
        else:
            RUNTIME.mark('inventory','UNAVAILABLE','LME/SHFE',data_at=None,used=False,reliability=0.0,alternative='official sources retried this run; diagnostic free supply proxy excluded from score')
    else:
        inv={k:prev_phys.get(k) for k in ('lmeInventoryTonnes','shfeInventoryTonnes','sources','statuses','collectorVersion','dataAt','cadence','lmeMonthlyHistory')}
        inv['sources']=inv.get('sources') or {}; inv['statuses']=inv.get('statuses') or {}
        if prev_official:
            RUNTIME.mark('inventory','CACHE','LME/SHFE',data_at=PREVIOUS_PAYLOAD.get('generatedAt'),used=True,reliability=0.90,ttl_s=24*3600)
        else:
            RUNTIME.mark('inventory','UNAVAILABLE','LME/SHFE',data_at=None,used=False,reliability=0.0,alternative='official exchange access remains blocked/unavailable; next retry cadence 6h')

    shared_copper=(price.get('dailyCandles') or [])
    china=china_cycle_proxy(shared_copper)
    if china.get('manufacturingConstructionScore') is None: china=lkg_or_unavailable('china','Yahoo Finance',max_age_h=8) or china
    else: RUNTIME.mark('china','LIVE','Yahoo Finance',data_at=now.isoformat())
    conc=concentrate_proxy(shared_copper)
    if conc.get('concentrateTightnessProxy') is None:
        conc=lkg_or_unavailable('concentrate','Yahoo Finance',max_age_h=8,used=False) or conc
    else:
        RUNTIME.mark('concentrate','LIVE','Yahoo Finance',data_at=now.isoformat(),used=False,reliability=0.7,alternative='diagnostic-only; excluded from V3 score')

    prev_supply=previous_section('supply') or {}
    supply_cache_valid=prev_supply.get('filterVersion')==SUPPLY_FILTER_VERSION
    # Old cached supply output used an over-broad query and may contain unrelated
    # real-estate/force-majeure stories. Never reuse it after the filter upgrade.
    if is_due('supply',1) or not supply_cache_valid:
        try:
            dis=disruptions()
        except Exception:
            # Only reuse an LKG if it was produced by the current copper relevance filter.
            lg=lkg_or_unavailable('supply','Google News RSS',max_age_h=24)
            dis=lg if isinstance(lg,dict) and lg.get('filterVersion')==SUPPLY_FILTER_VERSION else {
                "supplyDisruptionScore":None,"events":[],"eventCount":0,
                "filterVersion":SUPPLY_FILTER_VERSION,
                "source":"Google News RSS; copper-relevance filter unavailable"
            }
        if dis.get('supplyDisruptionScore') is not None:
            RUNTIME.mark('supply','LIVE','Google News RSS',data_at=now.isoformat(),used=True,reliability=0.75,alternative='event proxy; not verified disrupted tonnage',ttl_s=3600)
        else:
            RUNTIME.mark('supply','UNAVAILABLE','Google News RSS',data_at=now.isoformat(),used=False,reliability=0.0)
    else:
        dis=prev_supply
        RUNTIME.mark('supply','CACHE','Google News RSS',data_at=PREVIOUS_PAYLOAD.get('generatedAt'),used=True,reliability=0.70,alternative='cached event proxy; not verified disrupted tonnage',ttl_s=3600)

    proxy=free_inventory_proxy(curve,china,conc)
    total,chg13,pct,official_obs=history_inventory(today,inv,proxy)
    inventory_mode='official' if total is not None else 'official_unavailable'
    physical={
      **inv,"visibleInventoryTonnes":total,"inventoryChangePct13w":chg13,"inventoryScore":pct,
      "inventoryMode":inventory_mode,"officialObservationCount":official_obs,
      "inventoryDataAt":max([v for v in (inv.get('dataAt') or {}).values() if v],default=None),
      "inventoryCadence":inv.get('cadence') or {},
      "freeSupplyProxyScore":proxy.get('inventoryScore'),"freeSupplyProxyTrendEquivalent":proxy.get('inventoryChangePct13w'),
      "inventoryProxySource":proxy.get('source'),"chinaDemandProxyScore":china.get('chinaDemandProxyScore'),
      "curveSpreadPct":curve.get('curveSpreadPct'),"curveScore":curve.get('curveScore'),
      "concentrateTightnessProxy":conc.get('concentrateTightnessProxy')
    }
    if inventory_mode=='official':
        inv_dates=[v for v in (inv.get('dataAt') or {}).values() if v]
        actual_at=max(inv_dates) if inv_dates else today
        state='LIVE' if actual_at==today else 'CACHE'
        RUNTIME.mark('physical',state,'LME/SHFE',data_at=actual_at,used=True,reliability=1.0 if state=='LIVE' else 0.9,ttl_s=45*86400)
    else:
        RUNTIME.mark('physical','UNAVAILABLE','LME/SHFE',data_at=None,used=False,reliability=0.0,alternative='diagnostic free supply proxy exists but is not official inventory')
        RUNTIME.mark('free_supply_proxy','FALLBACK','Yahoo Finance',data_at=now.isoformat(),used=False,reliability=0.70,alternative='COMEX curve + FXI China proxy + concentrate proxy')

    payload={"schemaVersion":"1.0","modelVersion":ENGINE_MODEL_VERSION,"generatedAt":now.isoformat(),"engine":"copper-cycle-engine","price":price,
             "physical":physical,"curve":curve,"cot":cot,"china":china,"concentrate":conc,"supply":dis,
             "apiHealth":RUNTIME.health(),
             "notes":["No paid data used","Official LME/SHFE inventory is never replaced by a proxy in official inventory fields",
                      "Free supply proxy is diagnostic-only when official inventory is unavailable",
                      "China cycle score uses FXI only; copper price momentum is diagnostic-only",
                      "13-week inventory change uses a calendar 91-day lag rather than observation count",
                      "Mine disruption score requires copper-specific context and is deduplicated news-event evidence, not a tonnage estimate","Public LME monthly stock-summary XLSX files are fetched directly to seed official inventory history when fewer than four official observations exist","Unavailable official inventory is retried on a 6-hour cadence until an exchange value is recovered","Supply cache is invalidated when the copper-relevance filter version changes","Supply events are clustered by copper asset so duplicate restart/outage stories do not multiply the score","Official LME monthly inventory is accepted as a lower-cadence official fallback with its true publication date when daily access is blocked","Google News supply events are hard-filtered by parsed publication date to the last 14 days because RSS query windows can return stale articles","Unauthenticated LME/SHFE official inventory may remain unavailable because LME report downloads can require account access and SHFE may present anti-bot verification; the engine never substitutes a proxy into official fields"]}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    API_HEALTH.write_text(json.dumps(RUNTIME.health(),ensure_ascii=False,indent=2))

    # V3 inventory evidence enrichment is part of the collector transaction.
    # This guarantees GitHub Actions cannot accidentally publish a base payload
    # without the three inventory-derived dashboard fields. Official exchange
    # data remains first priority; a clearly-labelled LME stock mirror is the
    # last-resort evidence path when LME/SHFE/CME block the runner.
    enrich_result=None
    try:
        from official_inventory_enricher import run as enrich_official_inventory
        enrich_result=enrich_official_inventory()
    except Exception as e:
        enrich_result={"ok":False,"error":f"{type(e).__name__}: {e}"}

    print(json.dumps({"ok":True,"out":str(OUT),"generatedAt":payload['generatedAt'],"apiHealth":str(API_HEALTH),"officialInventoryEnrichment":enrich_result},ensure_ascii=False))
if __name__=='__main__':main()
