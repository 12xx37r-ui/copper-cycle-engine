from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "public/data/copper_fundamentals.json"
CFTC_URLS = (
    "https://publicreportinghub.cftc.gov/resource/72hh-3qpy.json",
    "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",
)
COPPER_CFTC_CODE = "085692"


def _num(value: Any) -> float | None:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _pct_rank(values: list[float], current: float | None) -> float | None:
    vals = [float(x) for x in values if _num(x) is not None]
    if not vals or current is None:
        return None
    return 100.0 * sum(x <= current for x in vals) / len(vals)


def _fetch_copper_cot() -> dict[str, Any] | None:
    fields = ",".join([
        "report_date_as_yyyy_mm_dd",
        "market_and_exchange_names",
        "cftc_contract_market_code",
        "open_interest_all",
        "m_money_positions_long_all",
        "m_money_positions_short_all",
        "other_rept_positions_long",
        "other_rept_positions_short",
    ])
    params = {
        "$select": fields,
        "$where": f"cftc_contract_market_code='{COPPER_CFTC_CODE}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "260",
    }
    rows: list[dict[str, Any]] = []
    for url in CFTC_URLS:
        try:
            r = requests.get(url, params=params, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            rows = r.json()
            if rows:
                break
        except Exception:
            rows = []
    if not rows:
        return None

    parsed: list[dict[str, Any]] = []
    for r in rows:
        dt = r.get("report_date_as_yyyy_mm_dd")
        oi = _num(r.get("open_interest_all"))
        mm_l = _num(r.get("m_money_positions_long_all"))
        mm_s = _num(r.get("m_money_positions_short_all"))
        or_l = _num(r.get("other_rept_positions_long"))
        or_s = _num(r.get("other_rept_positions_short"))
        if not dt or not oi or None in (mm_l, mm_s, or_l, or_s):
            continue
        net_contracts = (mm_l - mm_s) + (or_l - or_s)
        parsed.append({
            "date": dt,
            "openInterest": oi,
            "specNetContracts": net_contracts,
            "specNetPctOfOI": net_contracts / oi * 100.0,
            "managedMoneyLong": mm_l,
            "managedMoneyShort": mm_s,
            "otherReportablesLong": or_l,
            "otherReportablesShort": or_s,
            "market": r.get("market_and_exchange_names"),
        })
    if not parsed:
        return None

    latest = parsed[0]
    prior = parsed[1] if len(parsed) > 1 else latest
    latest["openInterestChangePct"] = ((latest["openInterest"] / prior["openInterest"] - 1.0) * 100.0 if prior.get("openInterest") else None)
    latest["specNetChangeContracts"] = latest["specNetContracts"] - prior["specNetContracts"]
    latest["specNetChangePp"] = latest["specNetPctOfOI"] - prior["specNetPctOfOI"]
    latest["cotPercentile"] = _pct_rank([x["specNetPctOfOI"] for x in parsed], latest["specNetPctOfOI"])
    latest["source"] = "CFTC Public Reporting Hub · Disaggregated Futures Only"
    latest["displayLabel"] = "COT 투기성 순포지션"
    latest["displayDefinition"] = "Managed Money와 Other Reportables의 순포지션 합계"
    latest["dataAt"] = latest["date"]
    latest["checkedAt"] = datetime.now(timezone.utc).isoformat()
    latest["cadence"] = "weekly"
    latest["netPct"] = latest["specNetPctOfOI"]
    latest["netChangePp"] = latest["specNetChangePp"]
    return latest


def _find_latest_date(value: Any) -> str | None:
    dates: list[str] = []
    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                kl = str(k).lower()
                if isinstance(x, str) and ("date" in kl or kl.endswith("at")):
                    s = x[:10]
                    try:
                        datetime.fromisoformat(s)
                        dates.append(s)
                    except Exception:
                        pass
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(value)
    return max(dates) if dates else None


def _normalize_inventory(payload: dict[str, Any]) -> None:
    physical = payload.get("physical") or {}
    derived = physical.get("officialInventoryDerived") or {}
    evidence_class = str(derived.get("evidenceClass") or "").lower()
    basket = str(derived.get("basket") or "").lower()
    data_at = physical.get("inventoryDataAt") or (physical.get("dataAt") or {}).get("lme") or _find_latest_date(derived)
    if data_at:
        physical["inventoryDataAt"] = str(data_at)[:10]

    is_mirror = "mirror" in evidence_class or "mirror" in basket
    is_lkg = str(derived.get("status") or "").lower() in {"lkg", "last_good", "last-known-good"}
    official_ok = bool(derived.get("officialSourceAvailable")) and not is_mirror
    if is_lkg:
        badge, source_badge = "임시 저장 자료", None
    elif official_ok:
        badge, source_badge = "최신 정상 자료", None
    elif is_mirror:
        badge, source_badge = "최신 자료", "2차자료"
    else:
        badge, source_badge = "최신 자료", None

    physical["displayMeta"] = {
        "badge": badge,
        "sourceBadge": source_badge,
        "dataAt": str(data_at)[:10] if data_at else None,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }
    payload["physical"] = physical


def _normalize_supply(payload: dict[str, Any]) -> None:
    supply = payload.get("supply") or {}
    score = _num(supply.get("supplyDisruptionScore"))
    if score is None:
        supply["displayValue"] = "확인 필요"
        supply["displayTone"] = "muted"
    elif score == 0:
        supply["displayValue"] = "광산 정상"
        supply["displayTone"] = "yellow"
    else:
        supply["displayValue"] = score
        supply["displayTone"] = "default"
    payload["supply"] = supply


def run() -> dict[str, Any]:
    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    cot = _fetch_copper_cot()
    if cot:
        payload["cot"] = cot
    _normalize_inventory(payload)
    _normalize_supply(payload)
    payload.setdefault("displayPolicy", {})["hideInternalIdentifiers"] = True
    PAYLOAD.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "cotUpdated": bool(cot),
        "cotDate": (cot or {}).get("date"),
        "cotNetContracts": (cot or {}).get("specNetContracts"),
        "inventoryDataAt": (payload.get("physical") or {}).get("inventoryDataAt"),
        "supplyDisplay": (payload.get("supply") or {}).get("displayValue"),
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
