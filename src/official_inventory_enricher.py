from __future__ import annotations

import io
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_PATH = ROOT / "public/data/copper_fundamentals.json"
HISTORY_PATH = ROOT / "public/data/official_inventory_history.json"
API_HEALTH_PATH = ROOT / "public/data/api_health.json"
STATE_PATH = ROOT / "public/data/official_inventory_state.json"
OFFICIAL_RETRY_HOURS = 6
MIRROR_RETRY_HOURS = 6

COLLECTOR_VERSION = "COPPER_INVENTORY_EVIDENCE_V3_20260819"
MIN_PERCENTILE_OBS = 12
LAG_DAYS = 91
HISTORY_YEARS = 5
MIRROR_MAX_AGE_DAYS = 21
WESTMETALL_BASE = "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Cu_cash"

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

LME_STOCKS_SUMMARY_PAGE = "https://www.lme.com/Market-data/Reports-and-data/Warehouse-and-stocks-reports/Stocks-summary"
LME_QUEUE_PAGE = "https://www.lme.com/en/market-data/reports-and-data/warehouse-and-stocks-reports/warehouse-and-queue-data"
SHFE_WEEKLY_PAGE = "https://www.shfe.com.cn/eng/reports/StatisticalData/WeeklyData/"
CME_COPPER_STOCKS = "https://www.cmegroup.com/delivery_reports/Copper_Stocks.xls"

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def _num(v: Any) -> float | None:
    try:
        x = float(str(v).replace(",", "").strip())
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _month_end(y: int, m: int) -> date:
    n = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return n - timedelta(days=1)


def _request(url: str, timeout: int = 25) -> requests.Response:
    h = dict(UA)
    if "lme.com" in url:
        h["Referer"] = LME_STOCKS_SUMMARY_PAGE
    elif "shfe.com.cn" in url:
        h["Referer"] = SHFE_WEEKLY_PAGE
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


def _tables(resp: requests.Response) -> list[pd.DataFrame]:
    ctype = (resp.headers.get("content-type") or "").lower()
    url = str(getattr(resp, "url", ""))
    raw = resp.content
    out: list[pd.DataFrame] = []
    if "spreadsheet" in ctype or re.search(r"\.(xlsx?|xls)(?:\?|$)", url, re.I):
        try:
            sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None)
            out.extend(sheets.values())
            return out
        except Exception:
            # Some reports have a real header row and parse better with the default.
            try:
                sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None)
                out.extend(sheets.values())
                return out
            except Exception:
                return []
    try:
        out.extend(pd.read_html(io.StringIO(resp.text), header=None))
    except Exception:
        pass
    return out


def _row_text(row: Iterable[Any]) -> str:
    return " ".join(str(x) for x in row if str(x).lower() != "nan")


def _copper_total_from_table(df: pd.DataFrame) -> float | None:
    """Extract a copper stock total from LME/SHFE-style tables.

    We deliberately require a copper-labelled row/section and plausible tonne values.
    No proxy or price-derived value can enter this parser.
    """
    if df is None or df.empty:
        return None

    for ridx in range(len(df)):
        row = list(df.iloc[ridx].values)
        text = _row_text(row)
        if not re.search(r"\bcopper\b|阴极铜|陰極銅|沪铜|铜", text, re.I):
            continue

        # First try same-row values. Exchange summaries commonly put totals here.
        vals = [x for x in (_num(v) for v in row) if x is not None and 500 <= x <= 10_000_000]
        if vals:
            # The last large numeric cell is usually Total/Closing Stock. Choosing the
            # last, rather than max(), avoids selecting cumulative movement columns.
            return vals[-1]

        # Some XLSX layouts use a "COPPER" section header followed by totals.
        for j in range(ridx + 1, min(ridx + 8, len(df))):
            r2 = list(df.iloc[j].values)
            t2 = _row_text(r2).lower()
            if re.search(r"\baluminium\b|\bzinc\b|\bnickel\b|\blead\b|\btin\b", t2):
                break
            vals2 = [x for x in (_num(v) for v in r2) if x is not None and 500 <= x <= 10_000_000]
            if vals2 and re.search(r"total|closing|stock|inventory|库存|庫存", t2, re.I):
                return vals2[-1]
    return None


def _cme_copper_total_from_table(df: pd.DataFrame) -> float | None:
    """Parse CME COMEX Copper_Stocks.xls.

    CME reports can be warehouse rows rather than one copper-labelled row. Sum the
    exchange report's TOTAL registered+eligible stock when present; otherwise sum
    warehouse rows conservatively. Unit remains whatever the official report states,
    so COMEX is never numerically mixed with LME/SHFE unless a future parser explicitly
    normalises units. It is kept as its own basket/component.
    """
    if df is None or df.empty:
        return None

    rows = [list(df.iloc[i].values) for i in range(len(df))]
    # Prefer an explicit TOTAL row.
    for row in rows:
        text = _row_text(row).strip().lower()
        if re.search(r"\btotal\b", text):
            vals = [x for x in (_num(v) for v in row) if x is not None and x >= 0]
            if vals:
                return vals[-1]
    return None


def _extract_links(html: str, base: str, label_re: str) -> list[tuple[str, str]]:
    from urllib.parse import urljoin
    out: list[tuple[str, str]] = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or "", re.I | re.S):
        href = urljoin(base, m.group(1))
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        label = re.sub(r"\s+", " ", label).strip()
        if re.search(label_re, label, re.I):
            out.append((label, href))
    return out


def _lme_direct_url(y: int, m: int) -> str:
    return (
        "https://www.lme.com/-/media/files/data/reports-and-data/"
        f"warehouse-and-stock-reports/stocks-summary/stocks-{MONTHS[m-1]}-{y}.xlsx"
    )


def _lme_queue_direct_url(y: int, m: int) -> str:
    return (
        "https://www.lme.com/-/media/files/data/reports-and-data/warehouse-and-stock-reports/"
        f"warehouse-stocks-and-queue/warehouse-company-stocks-and-queue-data-{MONTHS[m-1]}-{y}.xlsx"
    )


def collect_lme_monthly(months_back: int = 60) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect official LME monthly copper closing stocks.

    Direct Stocks Summary XLSX is first choice. Warehouse/queue XLSX is a second
    official LME route. The function records errors instead of inventing values.
    """
    today = date.today()
    y, m = today.year, today.month - 1
    if m == 0:
        y, m = y - 1, 12
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for _ in range(months_back):
        found = None
        source_url = None
        route = None
        for name, url in (
            ("lme_stocks_summary", _lme_direct_url(y, m)),
            ("lme_warehouse_queue", _lme_queue_direct_url(y, m)),
        ):
            try:
                rr = _request(url, timeout=30)
                for t in _tables(rr):
                    found = _copper_total_from_table(t)
                    if found is not None:
                        break
                if found is not None:
                    source_url, route = url, name
                    break
                errors.append(f"{y}-{m:02d} {name}: parsed no copper total")
            except Exception as e:
                errors.append(f"{y}-{m:02d} {name}: {type(e).__name__}")

        if found is not None:
            rows.append({
                "date": _month_end(y, m).isoformat(),
                "components": {"lme": found},
                "basket": "lme",
                "totalTonnes": found,
                "cadence": "monthly",
                "source": "LME official monthly stock report",
                "sourceUrl": source_url,
                "route": route,
            })

        m -= 1
        if m == 0:
            y, m = y - 1, 12

    rows.sort(key=lambda x: x["date"])
    return rows, errors



def _parse_westmetall_html(html: str, source_url: str) -> list[dict[str, Any]]:
    """Parse Westmetall's daily LME Copper stock history robustly.

    Westmetall mirrors LME Copper cash/3m prices and the LME Copper stock column.
    The HTML layout can be rendered with repeated header rows, flattened columns,
    or generic integer column labels depending on pandas/lxml versions.  Therefore
    this parser does NOT depend on DataFrame column names.  A valid observation must
    have a leading calendar date plus at least three numeric cells; the final numeric
    cell is the stock column.  This mirrors the public datatable layout exactly.
    """
    out: list[dict[str, Any]] = []
    if not html:
        return out

    def add_row(raw_date: Any, raw_values: Iterable[Any]) -> None:
        text = str(raw_date).strip()
        # Require an actual Westmetall-style day/month/year date; this prevents
        # repeated header rows and unrelated numeric tables from being accepted.
        if not re.search(r'\b\d{1,2}\.?\s+[A-Za-z]+\s+20\d{2}\b', text):
            return
        try:
            dt = pd.to_datetime(text, dayfirst=True, errors='raise').date()
        except Exception:
            return
        vals = []
        for value in raw_values:
            v = _num(value)
            if v is not None:
                vals.append(v)
        # Datatable order is cash, 3-month, stock.  The stock field is the final
        # numeric cell and should be plausible LME tonnes.
        if len(vals) < 3:
            return
        stock = vals[-1]
        if not (1_000 <= stock <= 5_000_000):
            return
        out.append({
            'date': dt.isoformat(),
            'components': {'lme': stock},
            'basket': 'lme_mirror',
            'totalTonnes': stock,
            'cadence': 'daily',
            'source': 'Westmetall mirror of LME Copper stock',
            'sourceClass': 'exchange_mirror',
            'officialUnderlying': 'LME',
            'sourceUrl': source_url,
        })

    # Route 1: pandas tables, independent of whatever header pandas inferred.
    try:
        for df in pd.read_html(io.StringIO(html), header=None):
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                cells = list(row.values)
                if len(cells) < 4:
                    continue
                add_row(cells[0], cells[1:])
    except Exception:
        pass

    # Route 2: parse individual HTML rows/cells. This survives table/header changes.
    if not out:
        for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', html, re.I | re.S):
            cells = re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', tr, re.I | re.S)
            clean_cells=[]
            for c in cells:
                c=re.sub(r'<[^>]+>', ' ', c)
                c=re.sub(r'&nbsp;|&#160;', ' ', c, flags=re.I)
                c=re.sub(r'\s+', ' ', c).strip()
                clean_cells.append(c)
            if len(clean_cells) >= 4:
                add_row(clean_cells[0], clean_cells[1:])

    # Route 3: textual fallback for proxies/CDNs that flatten the table markup.
    if not out:
        plain = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', html, flags=re.I|re.S)
        plain = re.sub(r'<[^>]+>', ' | ', plain)
        plain = re.sub(r'&nbsp;|&#160;', ' ', plain, flags=re.I)
        plain = re.sub(r'\s+', ' ', plain)
        pat = re.compile(
            r'(\d{1,2}\.?\s+[A-Za-z]+\s+20\d{2})\s*\|\s*'
            r'([\d,]+(?:\.\d+)?)\s*\|\s*([\d,]+(?:\.\d+)?)\s*\|\s*([\d,]+)',
            re.I,
        )
        for m in pat.finditer(plain):
            add_row(m.group(1), (m.group(2), m.group(3), m.group(4)))

    by_date = {r['date']: r for r in out}
    return [by_date[k] for k in sorted(by_date)]

def collect_lme_mirror(years_back: int = HISTORY_YEARS) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect a machine-readable mirror of LME Copper stocks as a last resort.

    Direct official exchange retrieval always has priority.  The mirror is useful
    for percentile and 13-week direction when LME's files are blocked to an
    unauthenticated GitHub runner.  It is never labelled as an official source.
    """
    today = date.today()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for y in range(today.year, today.year - years_back - 1, -1):
        # The unqualified URL is Westmetall's canonical current-year page; older
        # years use the explicit `year=` query parameter. Trying both for the
        # current year makes the collector tolerant of server-side query handling.
        urls = [WESTMETALL_BASE] if y == today.year else []
        urls.append(f"{WESTMETALL_BASE}&year={y}")
        year_rows=[]
        year_errors=[]
        for url in dict.fromkeys(urls):
            try:
                rr = _request(url, timeout=25)
                parsed = _parse_westmetall_html(rr.text, url)
                if parsed:
                    year_rows.extend(parsed)
                    break
                year_errors.append(f"parsed no LME Copper stock rows: {url}")
            except Exception as e:
                year_errors.append(f"{type(e).__name__}: {url}")
        if not year_rows:
            errors.append(f"Westmetall {y}: {'; '.join(year_errors)}")
        rows.extend(year_rows)

    by_date = {r["date"]: r for r in rows}
    rows = [by_date[k] for k in sorted(by_date)]
    if rows:
        latest = date.fromisoformat(rows[-1]["date"])
        age = (today - latest).days
        if age > MIRROR_MAX_AGE_DAYS:
            errors.append(f"Westmetall mirror stale: latest {latest.isoformat()} age {age}d")
            return [], errors
    return rows, errors

def collect_shfe_current() -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    urls = [
        SHFE_WEEKLY_PAGE,
        "https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/?query_options=1&query_params=inventory",
    ]
    for url in urls:
        try:
            rr = _request(url, timeout=30)
            body = (rr.text or "")[:10000].lower()
            if "人机识别" in body or "captcha" in body or "web 应用防火墙" in body:
                errors.append(f"SHFE WAF/challenge: {url}")
                continue
            for t in _tables(rr):
                v = _copper_total_from_table(t)
                if v is not None:
                    return ({
                        "date": date.today().isoformat(),
                        "components": {"shfe": v},
                        "basket": "shfe",
                        "totalTonnes": v,
                        "cadence": "weekly",
                        "source": "SHFE official weekly inventory",
                        "sourceUrl": url,
                    }, errors)
            errors.append(f"SHFE parsed no copper total: {url}")
        except Exception as e:
            errors.append(f"SHFE {type(e).__name__}: {url}")
    return None, errors


def collect_comex_current() -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    try:
        rr = _request(CME_COPPER_STOCKS, timeout=30)
        for t in _tables(rr):
            v = _cme_copper_total_from_table(t)
            if v is not None:
                return ({
                    "date": date.today().isoformat(),
                    "components": {"comex": v},
                    "basket": "comex",
                    "totalOfficialReportUnits": v,
                    "cadence": "daily",
                    "source": "CME/COMEX official Copper Stocks report",
                    "sourceUrl": CME_COPPER_STOCKS,
                }, errors)
        errors.append("CME Copper_Stocks.xls parsed no explicit TOTAL")
    except Exception as e:
        errors.append(f"CME/COMEX {type(e).__name__}")
    return None, errors



def _read_state() -> dict[str, Any]:
    try:
        x=json.loads(STATE_PATH.read_text())
        return x if isinstance(x,dict) else {}
    except Exception:
        return {}

def _write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True,exist_ok=True)
    STATE_PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2))

def _official_attempt_due(state: dict[str, Any]) -> bool:
    raw=state.get('lastOfficialAttemptAt')
    if not raw:
        return True
    try:
        dt=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds() >= OFFICIAL_RETRY_HOURS*3600
    except Exception:
        return True

def _mirror_attempt_due(state: dict[str, Any]) -> bool:
    raw=state.get('lastMirrorAttemptAt')
    if not raw:
        return True
    try:
        dt=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds() >= MIRROR_RETRY_HOURS*3600
    except Exception:
        return True

def _read_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    try:
        x = json.loads(path.read_text())
        return x if isinstance(x, list) else []
    except Exception:
        return []


def _write_history(rows: list[dict[str, Any]], path: Path = HISTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))


def merge_history(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge observations by (date,basket), never mixing incompatible baskets."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    cutoff = date.today() - timedelta(days=366 * HISTORY_YEARS + 40)
    for g in groups:
        for row in g or []:
            if not isinstance(row, dict) or not row.get("date") or not row.get("basket"):
                continue
            try:
                d = date.fromisoformat(str(row["date"]))
            except Exception:
                continue
            if d < cutoff:
                continue
            by_key[(str(row["date"]), str(row["basket"]))] = row
    return [by_key[k] for k in sorted(by_key)]


def _metric_value(row: dict[str, Any]) -> float | None:
    if row.get("basket") == "comex":
        return _num(row.get("totalOfficialReportUnits"))
    return _num(row.get("totalTonnes"))


def compute_basket_metrics(history: list[dict[str, Any]], basket: str, current_date: str | None = None) -> dict[str, Any]:
    rows = [r for r in history if r.get("basket") == basket and _metric_value(r) is not None]
    rows.sort(key=lambda x: x["date"])
    if current_date:
        rows = [r for r in rows if r["date"] <= current_date]
    if not rows:
        return {"basket": basket, "status": "unavailable", "observationCount": 0}

    current = rows[-1]
    cur = _metric_value(current)
    latest_date = date.fromisoformat(current["date"])
    target = latest_date - timedelta(days=LAG_DAYS)
    eligible = [r for r in rows[:-1] if date.fromisoformat(r["date"]) <= target]
    prior = eligible[-1] if eligible else None
    prior_v = _metric_value(prior) if prior else None
    change = (cur / prior_v - 1) * 100 if cur and prior_v else None

    vals = [_metric_value(r) for r in rows]
    vals = [v for v in vals if v is not None]
    percentile = None
    if len(vals) >= MIN_PERCENTILE_OBS:
        percentile = 100.0 * sum(v <= cur for v in vals) / len(vals)

    # Dashboard long-term direction convention: falling inventory is tighter /
    # more bullish pressure. 50 neutral, +/-2 score points per 1% inventory move,
    # bounded to 0..100. This is explicitly a derived direction score.
    trend_score = None if change is None else max(0.0, min(100.0, 50.0 - change * 2.0))

    return {
        "basket": basket,
        "status": "ok" if change is not None else "insufficient_history",
        "dataAt": current["date"],
        "currentValue": cur,
        "currentComponents": current.get("components") or {},
        "cadence": current.get("cadence"),
        "source": current.get("source"),
        "sourceUrl": current.get("sourceUrl"),
        "observationCount": len(rows),
        "percentile": percentile,
        "percentileStatus": "ok" if percentile is not None else "insufficient_history",
        "changePct13w": change,
        "prior91dDate": prior.get("date") if prior else None,
        "prior91dValue": prior_v,
        "trendScore13w": trend_score,
    }


def choose_primary_metrics(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the best same-basket official series.

    Priority is LME+SHFE (if ever collected as one matched basket), then LME, then
    SHFE, then COMEX. We never compare current LME against historical LME+SHFE.
    """
    priority = ["lme_shfe", "lme", "shfe", "comex", "lme_mirror"]
    candidates = [compute_basket_metrics(history, b) for b in priority]
    usable = [x for x in candidates if x.get("currentValue") is not None]
    if not usable:
        return {"status": "unavailable", "basket": None, "candidates": candidates}

    # Prefer a series capable of producing 13w change, then percentile, then priority.
    def quality(x: dict[str, Any]) -> tuple[int, int, int]:
        return (
            1 if x.get("changePct13w") is not None else 0,
            1 if x.get("percentile") is not None else 0,
            -priority.index(x["basket"]),
        )
    chosen = max(usable, key=quality)
    chosen = dict(chosen)
    chosen["candidates"] = candidates
    return chosen


def apply_to_payload(payload: dict[str, Any], metrics: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    physical = payload.setdefault("physical", {})
    evidence_class = "exchange_mirror" if metrics.get("basket") == "lme_mirror" else "official_exchange"
    official = {
        "collectorVersion": COLLECTOR_VERSION,
        "status": metrics.get("status"),
        "basket": metrics.get("basket"),
        "evidenceClass": evidence_class,
        "officialSourceAvailable": evidence_class == "official_exchange",
        "dataAt": metrics.get("dataAt"),
        "cadence": metrics.get("cadence"),
        "source": metrics.get("source"),
        "sourceUrl": metrics.get("sourceUrl"),
        "observationCount": metrics.get("observationCount", 0),
        "visibleInventoryPercentile": metrics.get("percentile"),
        "inventoryChangePct13w": metrics.get("changePct13w"),
        "inventoryTrendScore13w": metrics.get("trendScore13w"),
        "prior91dDate": metrics.get("prior91dDate"),
        "currentComponents": metrics.get("currentComponents") or {},
        "errors": errors[-30:],
    }
    physical["officialInventoryDerived"] = official

    # Only populate existing dashboard fields when the metric is truly official.
    # COMEX remains a separately-labelled basket because its report unit semantics
    # differ from LME/SHFE tonnage; the dashboard can still consume the percentile/
    # trend, but visibleInventoryTonnes is not falsely labelled tonnes.
    if metrics.get("basket") in {"lme", "shfe", "lme_shfe", "lme_mirror"}:
        physical["visibleInventoryTonnes"] = metrics.get("currentValue")
    if metrics.get("percentile") is not None:
        physical["inventoryScore"] = metrics["percentile"]
    if metrics.get("changePct13w") is not None:
        physical["inventoryChangePct13w"] = metrics["changePct13w"]
    if metrics.get("currentValue") is not None:
        physical["inventoryMode"] = ("exchange_mirror_lme" if metrics.get("basket") == "lme_mirror" else f"official_{metrics['basket']}")
        physical["officialObservationCount"] = metrics.get("observationCount", 0) if evidence_class == "official_exchange" else 0
        physical["inventoryObservationCount"] = metrics.get("observationCount", 0)
        physical["inventoryDataAt"] = metrics.get("dataAt")
        physical["inventoryEvidenceClass"] = evidence_class

    # Explicit engine-level values for the four dashboard outputs.
    payload["officialIndicators"] = {
        "officialVisibleInventoryPercentile": metrics.get("percentile"),
        "officialInventoryChangePct13w": metrics.get("changePct13w"),
        "officialVisibleInventoryTrend13w": metrics.get("trendScore13w"),
        "mineSupplyDisruption": (payload.get("supply") or {}).get("supplyDisruptionScore"),
        "inventoryBasket": metrics.get("basket"),
        "inventoryDataAt": metrics.get("dataAt"),
        "inventoryObservationCount": metrics.get("observationCount", 0),
        "inventoryStatus": metrics.get("status"),
        "inventoryEvidenceClass": evidence_class,
        "officialSourceAvailable": evidence_class == "official_exchange",
        "inventorySource": metrics.get("source"),
        "inventorySourceUrl": metrics.get("sourceUrl"),
        "supplySource": (payload.get("supply") or {}).get("source"),
        "supplyEventCount": (payload.get("supply") or {}).get("eventCount"),
    }

    health = payload.setdefault("apiHealth", {}).setdefault("sources", {})
    if metrics.get("currentValue") is None:
        health["official_inventory_derived"] = {
            "status": "UNAVAILABLE", "source": "LME/SHFE/CME official reports",
            "dataAt": None, "usedInCalculation": False, "reliability": 0.0,
            "fallback": "No machine-readable official inventory observation recovered",
            "url": LME_STOCKS_SUMMARY_PAGE,
        }
    else:
        complete = metrics.get("changePct13w") is not None and metrics.get("percentile") is not None
        is_mirror = metrics.get("basket") == "lme_mirror"
        health["official_inventory_derived"] = {
            "status": "FALLBACK" if is_mirror else ("CACHE" if metrics.get("cadence") in {"monthly", "weekly"} else "LIVE"),
            "source": metrics.get("source"), "dataAt": metrics.get("dataAt"),
            "usedInCalculation": bool(complete),
            "reliability": (0.80 if complete else 0.65) if is_mirror else (0.95 if complete else 0.75),
            "fallback": ("Official LME/SHFE/CME retrieval blocked; using explicitly-labelled LME stock mirror" if is_mirror else (None if complete else "Official value recovered; history still insufficient for all derived metrics")),
            "url": metrics.get("sourceUrl"),
        }
    return payload


def run() -> dict[str, Any]:
    payload = json.loads(PAYLOAD_PATH.read_text())
    old = _read_history()
    state = _read_state()

    # The mirror is the practical history path when exchange anti-bot blocks
    # GitHub Actions. First successful run backfills five years; subsequent runs
    # fetch only the current year and merge into the committed local history.
    old_mirror=[r for r in old if isinstance(r,dict) and r.get('basket')=='lme_mirror']
    mirror_rows=[]; mirror_errors=[]
    # Westmetall mirrors a daily LME stock series. Do not fetch it on every
    # 30-minute workflow. Persisted history is enough between refresh windows.
    if _mirror_attempt_due(state):
        mirror_years = 0 if len(old_mirror) >= MIN_PERCENTILE_OBS else HISTORY_YEARS
        mirror_rows, mirror_errors = collect_lme_mirror(years_back=mirror_years)
        state['lastMirrorAttemptAt']=datetime.now(timezone.utc).isoformat()
        state['lastMirrorErrors']=mirror_errors[-10:]
        _write_state(state)
    else:
        mirror_errors=['LME mirror refresh cadence not due; using committed mirror history']

    lme_rows=[]; lme_errors=[]; shfe_current=None; shfe_errors=[]; comex_current=None; comex_errors=[]
    if _official_attempt_due(state):
        # Keep probing true exchange sources, but do not hammer dozens of blocked
        # historical URLs every 30-minute market run. The mirror supplies history;
        # official routes are retried on a six-hour cadence.
        lme_rows, lme_errors = collect_lme_monthly(months_back=4)
        shfe_current, shfe_errors = collect_shfe_current()
        comex_current, comex_errors = collect_comex_current()
        state['lastOfficialAttemptAt']=datetime.now(timezone.utc).isoformat()
        state['lastOfficialErrors']=(lme_errors+shfe_errors+comex_errors)[-20:]
        _write_state(state)
    else:
        lme_errors=['official exchange retry cadence not due; using committed history/current mirror']

    new_groups: list[list[dict[str, Any]]] = [lme_rows, mirror_rows]
    if shfe_current:
        new_groups.append([shfe_current])
    if comex_current:
        new_groups.append([comex_current])

    history = merge_history(old, *new_groups)
    _write_history(history)
    metrics = choose_primary_metrics(history)
    errors = lme_errors + shfe_errors + comex_errors + mirror_errors
    payload = apply_to_payload(payload, metrics, errors)
    # Record the actual enrichment check time so the health panel can distinguish
    # a fresh mirror observation from a stale one.
    h=((payload.get('apiHealth') or {}).get('sources') or {}).get('official_inventory_derived')
    if isinstance(h,dict):
        h['checkedAt']=datetime.now(timezone.utc).isoformat()
    PAYLOAD_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    API_HEALTH_PATH.write_text(json.dumps(payload.get("apiHealth") or {}, ensure_ascii=False, indent=2))

    result = {
        "ok": True,
        "collectorVersion": COLLECTOR_VERSION,
        "basket": metrics.get("basket"),
        "dataAt": metrics.get("dataAt"),
        "officialVisibleInventoryPercentile": metrics.get("percentile"),
        "officialInventoryChangePct13w": metrics.get("changePct13w"),
        "officialVisibleInventoryTrend13w": metrics.get("trendScore13w"),
        "mineSupplyDisruption": (payload.get("supply") or {}).get("supplyDisruptionScore"),
        "observationCount": metrics.get("observationCount", 0),
        "inventoryEvidenceClass": ("exchange_mirror" if metrics.get("basket") == "lme_mirror" else "official_exchange"),
        "inventorySource": metrics.get("source"),
        "officialRetryDue": _official_attempt_due(state),
        "errors": errors[-10:],
    }
    print(json.dumps(result, ensure_ascii=False))
    return result

if __name__ == '__main__':
    run()
