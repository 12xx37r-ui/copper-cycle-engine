import importlib.util
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

# collect.py source-level semantics: unavailable official observations must not claim dataAt.
collect=(ROOT/'src/collect.py').read_text()
assert "RUNTIME.mark('inventory','UNAVAILABLE','LME/SHFE',data_at=None" in collect
assert "RUNTIME.mark('physical','UNAVAILABLE','LME/SHFE',data_at=None" in collect

spec=importlib.util.spec_from_file_location('inv',ROOT/'src/official_inventory_enricher.py')
inv=importlib.util.module_from_spec(spec); spec.loader.exec_module(inv)

# Forced manual diagnostic bypasses cadence.
os.environ['COPPER_FORCE_OFFICIAL_INVENTORY']='1'
assert inv._official_attempt_due({'lastOfficialAttemptAt':'2999-01-01T00:00:00+00:00'}) is True
os.environ.pop('COPPER_FORCE_OFFICIAL_INVENTORY',None)

payload={'apiHealth':{'sources':{'inventory':{'status':'UNAVAILABLE','source':'LME/SHFE','fallback':'blocked','checkedAt':'2026-08-21T00:00:00+00:00'}}},'supply':{}}
metrics={'status':'ok','basket':'lme_mirror','currentValue':235975.0,'changePct13w':-40.0,'percentile':76.6,'trendScore13w':100.0,'observationCount':1293,'dataAt':'2026-08-19','cadence':'daily','source':'Westmetall mirror of LME Copper stock','sourceUrl':'https://example.com'}
out=inv.apply_to_payload(payload,metrics,[])
assert out['physical']['officialInventoryDerived']['status']=='FALLBACK'
assert out['inventoryEvidence']['status']=='FALLBACK'
assert out['inventoryEvidence']['officialDirectCheckedAt']=='2026-08-21T00:00:00+00:00'
print('freshness semantics v4 ok')
