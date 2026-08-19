from __future__ import annotations
import json, math, shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'public/data'
PAYLOAD=DATA/'copper_fundamentals.json'
HEALTH=DATA/'api_health.json'
BACKUP=ROOT/'.run-backup/copper_fundamentals.json'
LAST_INV=DATA/'last_good_inventory.json'
LAST_SUP=DATA/'last_good_supply.json'
INV_MAX_AGE_DAYS=21
SUP_MAX_AGE_HOURS=72


def _read(path, default=None):
    try:
        x=json.loads(path.read_text())
        return x
    except Exception:
        return default

def _write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2))

def _num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except Exception:return None

def _dt(v):
    if not v:return None
    try:
        d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:return None

def _age_hours(v):
    d=_dt(v)
    return None if d is None else max(0.0,(datetime.now(timezone.utc)-d).total_seconds()/3600)

def _inv_good(p):
    oi=(p or {}).get('officialIndicators') or {}
    return all(_num(oi.get(k)) is not None for k in (
        'officialVisibleInventoryPercentile','officialInventoryChangePct13w','officialVisibleInventoryTrend13w')) and bool(oi.get('inventoryDataAt'))

def _supply_good(p):
    # 0 is a valid observed state; None means unavailable.
    return _num(((p or {}).get('supply') or {}).get('supplyDisruptionScore')) is not None

def _inv_snapshot(p):
    return {
      'savedAt':datetime.now(timezone.utc).isoformat(),
      'generatedAt':p.get('generatedAt'),
      'physical':p.get('physical'),
      'officialIndicators':p.get('officialIndicators'),
      'health':(((p.get('apiHealth') or {}).get('sources') or {}).get('official_inventory_derived')),
    }

def _sup_snapshot(p):
    return {
      'savedAt':datetime.now(timezone.utc).isoformat(),
      'generatedAt':p.get('generatedAt'),
      'supply':p.get('supply'),
      'supplyIndicator':((p.get('officialIndicators') or {}).get('mineSupplyDisruption')),
      'health':(((p.get('apiHealth') or {}).get('sources') or {}).get('supply')),
    }

def _reliability_inventory(age_h):
    d=age_h/24.0
    if d<=3:return 0.80
    if d<=7:return 0.70
    if d<=14:return 0.50
    if d<=21:return 0.35
    return 0.0

def _restore_inventory(cur, snap):
    oi=(snap or {}).get('officialIndicators') or {}
    age_h=_age_hours(oi.get('inventoryDataAt'))
    if age_h is None or age_h>INV_MAX_AGE_DAYS*24:return False
    oldp=(snap or {}).get('physical') or {}
    p=cur.setdefault('physical',{})
    for k in ('visibleInventoryTonnes','inventoryChangePct13w','inventoryScore','inventoryMode','inventoryObservationCount','inventoryDataAt','inventoryEvidenceClass','officialInventoryDerived'):
        if k in oldp:p[k]=oldp[k]
    p['inventoryMode']='lkg_'+str(oldp.get('inventoryMode') or 'inventory')
    p['inventoryEvidenceClass']='LKG'
    cur['officialIndicators']=dict(oi)
    cur['officialIndicators']['inventoryStatus']='LKG'
    cur['officialIndicators']['inventoryEvidenceClass']='LKG'
    cur['officialIndicators']['inventoryLkgAgeHours']=round(age_h,1)
    h=cur.setdefault('apiHealth',{}).setdefault('sources',{})
    src=(snap or {}).get('health') or {}
    h['official_inventory_derived']={
      'status':'LKG','source':src.get('source') or oi.get('inventorySource') or 'last-good inventory',
      'dataAt':oi.get('inventoryDataAt'),'usedInCalculation':True,
      'reliability':_reliability_inventory(age_h),
      'fallback':'Current inventory routes failed; bounded last-good inventory evidence restored',
      'url':src.get('url') or oi.get('inventorySourceUrl'),
      'checkedAt':datetime.now(timezone.utc).isoformat(),
      'ageHours':round(age_h,1)
    }
    return True

def _restore_supply(cur,snap):
    s=(snap or {}).get('supply') or {}
    age_h=_age_hours((snap or {}).get('generatedAt') or (snap or {}).get('savedAt'))
    if age_h is None or age_h>SUP_MAX_AGE_HOURS or _num(s.get('supplyDisruptionScore')) is None:return False
    cur['supply']=s
    oi=cur.setdefault('officialIndicators',{})
    oi['mineSupplyDisruption']=s.get('supplyDisruptionScore')
    oi['supplySource']=s.get('source')
    oi['supplyEventCount']=s.get('eventCount')
    oi['supplyStatus']='LKG'
    oi['supplyLkgAgeHours']=round(age_h,1)
    src=(snap or {}).get('health') or {}
    cur.setdefault('apiHealth',{}).setdefault('sources',{})['supply']={
      'status':'LKG','source':src.get('source') or s.get('source') or 'last-good supply',
      'dataAt':(snap or {}).get('generatedAt'),'usedInCalculation':True,
      'reliability':max(0.35,0.75-0.4*min(age_h/SUP_MAX_AGE_HOURS,1.0)),
      'fallback':'Current supply-news collection failed; bounded last-good event state restored',
      'url':src.get('url'),'checkedAt':datetime.now(timezone.utc).isoformat(),'ageHours':round(age_h,1)
    }
    return True

def run():
    cur=_read(PAYLOAD,{}) or {}
    prev=_read(BACKUP,{}) or {}
    inv_last=_read(LAST_INV,{}) or {}
    sup_last=_read(LAST_SUP,{}) or {}
    actions=[]

    if _inv_good(cur):
        _write(LAST_INV,_inv_snapshot(cur)); actions.append('inventory:new-good')
    else:
        cand=_inv_snapshot(prev) if _inv_good(prev) else inv_last
        if _restore_inventory(cur,cand): actions.append('inventory:LKG-restored')
        else: actions.append('inventory:unavailable')

    supply_health=(((cur.get('apiHealth') or {}).get('sources') or {}).get('supply') or {})
    supply_current_valid=_supply_good(cur) and supply_health.get('status')!='UNAVAILABLE'
    if supply_current_valid:
        _write(LAST_SUP,_sup_snapshot(cur)); actions.append('supply:new-good')
    else:
        cand=_sup_snapshot(prev) if _supply_good(prev) else sup_last
        if _restore_supply(cur,cand): actions.append('supply:LKG-restored')
        else: actions.append('supply:unavailable')

    cur.setdefault('notes',[]).append('Final publish guard prevents transient source/workflow failures from overwriting valid inventory/supply indicators; bounded LKG expires after 21d inventory / 72h supply.')
    _write(PAYLOAD,cur)
    _write(HEALTH,cur.get('apiHealth') or {})
    result={'ok':True,'actions':actions,'inventoryGood':_inv_good(cur),'supplyGood':_supply_good(cur)}
    print(json.dumps(result,ensure_ascii=False))
    return result

if __name__=='__main__':run()
