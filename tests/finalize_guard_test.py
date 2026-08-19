import json, tempfile, importlib.util
from pathlib import Path
from datetime import datetime, timezone, timedelta
spec=importlib.util.spec_from_file_location('g',Path(__file__).parents[1]/'src/finalize_guard.py')
g=importlib.util.module_from_spec(spec); spec.loader.exec_module(g)

# Semantic core: 0 supply is valid, null is unavailable.
assert g._supply_good({'supply':{'supplyDisruptionScore':0}})
assert not g._supply_good({'supply':{'supplyDisruptionScore':None}})

# Inventory LKG can be restored when <=21 days old and all three metrics exist.
now=datetime.now(timezone.utc)
snap={'officialIndicators':{
 'officialVisibleInventoryPercentile':73.6,'officialInventoryChangePct13w':-43.3,
 'officialVisibleInventoryTrend13w':100.0,'inventoryDataAt':(now-timedelta(days=2)).date().isoformat(),
 'inventorySource':'fixture','inventorySourceUrl':'https://fixture','inventoryObservationCount':1294},
 'physical':{'visibleInventoryTonnes':223550,'inventoryChangePct13w':-43.3,'inventoryScore':73.6,'inventoryMode':'exchange_mirror_lme'},
 'health':{'source':'fixture','url':'https://fixture'}}
cur={'physical':{},'officialIndicators':{},'apiHealth':{'sources':{}}}
assert g._restore_inventory(cur,snap)
assert cur['officialIndicators']['inventoryStatus']=='LKG'
assert cur['officialIndicators']['officialVisibleInventoryPercentile']==73.6

# Expired inventory LKG is rejected.
snap['officialIndicators']['inventoryDataAt']=(now-timedelta(days=30)).date().isoformat()
assert not g._restore_inventory({'physical':{},'officialIndicators':{},'apiHealth':{'sources':{}}},snap)
print('finalize guard tests ok')
