from pathlib import Path
import importlib.util
import tempfile
import json

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("oi", ROOT / "src" / "official_inventory_enricher.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

# Same-basket 91-day lag + percentile + trend direction.
h=[]
for i,(d,v) in enumerate([
    ("2025-08-31",200.0),("2025-09-30",190.0),("2025-10-31",180.0),
    ("2025-11-30",175.0),("2025-12-31",170.0),("2026-01-31",165.0),
    ("2026-02-28",160.0),("2026-03-31",155.0),("2026-04-30",150.0),
    ("2026-05-31",145.0),("2026-06-30",140.0),("2026-07-31",135.0),
    ("2026-08-31",130.0),
]):
    h.append({"date":d,"basket":"lme","totalTonnes":v,"components":{"lme":v},"cadence":"monthly","source":"fixture"})
r=m.compute_basket_metrics(h,"lme")
assert r["observationCount"]==13, r
assert r["percentile"] is not None, r
assert r["prior91dDate"]=="2026-05-31", r
expected=(130.0/145.0-1)*100
assert abs(r["changePct13w"]-expected)<1e-9, r
assert r["trendScore13w"]>50, r  # falling inventory => tighter / bullish direction

# Basket mismatch must never be used for 13-week comparison.
mixed=[
    {"date":"2026-05-01","basket":"lme_shfe","totalTonnes":300,"components":{"lme":100,"shfe":200}},
    {"date":"2026-08-01","basket":"lme","totalTonnes":120,"components":{"lme":120}},
]
r2=m.compute_basket_metrics(mixed,"lme")
assert r2["changePct13w"] is None, r2
assert r2["status"]=="insufficient_history", r2

# Backfill without a current observation must not masquerade as today's value.
# The series dataAt remains the actual report date.
backfill=[
    {"date":"2026-04-30","basket":"lme","totalTonnes":150,"components":{"lme":150},"cadence":"monthly","source":"fixture"},
    {"date":"2026-05-31","basket":"lme","totalTonnes":145,"components":{"lme":145},"cadence":"monthly","source":"fixture"},
]
r3=m.compute_basket_metrics(backfill,"lme")
assert r3["dataAt"]=="2026-05-31", r3
assert r3["dataAt"] != m.date.today().isoformat() or m.date.today().isoformat()=="2026-05-31"

# LME-only and SHFE-only are valid baskets; neither needs the other to exist.
for basket,key in [("lme","lme"),("shfe","shfe")]:
    rows=[]
    for idx in range(13):
        d=m._month_end(2025 + (7+idx)//12, ((7+idx)%12)+1).isoformat()
        v=100+idx
        rows.append({"date":d,"basket":basket,"totalTonnes":v,"components":{key:v},"cadence":"monthly","source":"fixture"})
    rr=m.compute_basket_metrics(rows,basket)
    assert rr["currentComponents"].get(key) is not None, rr
    assert rr["percentile"] is not None, rr

# Payload enrichment exposes all four dashboard-facing outputs while preserving
# null instead of fabricating inventory when official data is unavailable.
p={"physical":{},"supply":{"supplyDisruptionScore":0,"eventCount":0,"source":"fixture"},"apiHealth":{"sources":{}}}
un=m.apply_to_payload(p,{"status":"unavailable","basket":None,"observationCount":0},["blocked"])
assert un["officialIndicators"]["officialVisibleInventoryPercentile"] is None
assert un["officialIndicators"]["officialInventoryChangePct13w"] is None
assert un["officialIndicators"]["officialVisibleInventoryTrend13w"] is None
assert un["officialIndicators"]["mineSupplyDisruption"]==0
assert un["physical"].get("visibleInventoryTonnes") is None

print("official inventory tests ok")

# Mirror fallback parser: explicitly-labelled LME stock mirror may fill derived
# metrics when official exchange downloads are blocked, but must not claim official.
fixture_html = '''
<table>
<tr><th>date</th><th>LME Copper Cash-Settlement</th><th>LME Copper 3-month</th><th>LME Copper stock</th></tr>
<tr><td>01. May 2026</td><td>12895.00</td><td>12967.00</td><td>398675</td></tr>
<tr><td>31. July 2026</td><td>13834.00</td><td>13800.00</td><td>249850</td></tr>
</table>
'''
mir = m._parse_westmetall_html(fixture_html, "https://fixture")
assert len(mir)==2, mir
assert mir[-1]["basket"]=="lme_mirror", mir[-1]
assert mir[-1]["totalTonnes"]==249850.0, mir[-1]

mirror_history=[]
base=m.date(2025,8,1)
for i in range(40):
    d=base + m.timedelta(days=i*14)
    v=400000-i*5000
    mirror_history.append({"date":d.isoformat(),"basket":"lme_mirror","totalTonnes":v,
        "components":{"lme":v},"cadence":"daily","source":"fixture mirror",
        "sourceUrl":"https://fixture"})
rm=m.compute_basket_metrics(mirror_history,"lme_mirror")
assert rm["percentile"] is not None, rm
assert rm["changePct13w"] is not None, rm
pp={"physical":{},"supply":{"supplyDisruptionScore":0,"eventCount":0,"source":"fixture"},"apiHealth":{"sources":{}}}
pp=m.apply_to_payload(pp,rm,[])
assert pp["officialIndicators"]["inventoryEvidenceClass"]=="exchange_mirror", pp
assert pp["officialIndicators"]["officialSourceAvailable"] is False, pp
assert pp["physical"]["inventoryMode"]=="exchange_mirror_lme", pp
assert pp["apiHealth"]["sources"]["official_inventory_derived"]["status"]=="FALLBACK", pp

# V3 collector identity and column-agnostic parser guard.
assert m.COLLECTOR_VERSION == "COPPER_INVENTORY_EVIDENCE_V4_4_20260821"
fixture_generic = """
<table><tbody>
<tr><td>07. August 2026</td><td>14,240.00</td><td>14,092.00</td><td>222,975</td></tr>
<tr><td>06. August 2026</td><td>14,455.00</td><td>14,260.00</td><td>226,650</td></tr>
</tbody></table>
"""
g = m._parse_westmetall_html(fixture_generic, "https://fixture-generic")
assert len(g) == 2, g
assert g[-1]["date"] == "2026-08-07", g
assert g[-1]["totalTonnes"] == 222975.0, g
print("official inventory tests ok · V3 robust Westmetall parser")
