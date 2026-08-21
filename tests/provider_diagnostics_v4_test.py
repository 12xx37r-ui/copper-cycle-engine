import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('collect_diag', ROOT/'src'/'collect.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class Resp:
    status_code=403
    headers={'content-type':'text/html; charset=utf-8'}
class E(Exception):
    response=Resp()

r=m._request_error_record(E('blocked'),'LME daily warehouse route','https://www.lme.com/test')
assert r['route']=='LME daily warehouse route'
assert r['statusCode']==403
assert r['contentType']=='text/html; charset=utf-8'
assert r['reason']=='authentication_or_waf_restriction'
assert r['exception']=='E'
assert r['checkedAt']

p=m._parse_failure_record('LME monthly deterministic XLSX','https://www.lme.com/test.xlsx',Resp())
assert p['exception']=='ParseFailure'
assert p['statusCode']==403
assert p['contentType']=='text/html; charset=utf-8'
assert p['reason']=='copper_inventory_not_found'

compact=m._request_error_detail(E('blocked'),'LME daily warehouse route','https://www.lme.com/test')
assert 'HTTP 403' in compact
assert 'authentication_or_waf_restriction' in compact
print('provider diagnostics v4 ok')
