import json,math
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'public/data/copper_fundamentals.json'
d=json.loads(p.read_text())
assert d['schemaVersion']=='1.0'
assert d['engine']=='copper-cycle-engine'
assert 'price' in d and 'physical' in d and 'curve' in d
for key in ['longPricePercentile','range1yPercentile']:
 v=d['price'].get(key)
 assert v is None or (math.isfinite(float(v)) and 0<=float(v)<=100)
print('validation ok')
