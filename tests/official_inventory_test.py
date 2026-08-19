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
