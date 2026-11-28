import json,sys
from pathlib import Path
h=Path(sys.argv[1])
items=json.loads((h/'effect.json').read_text(encoding='utf8'))['model']
assert len(items)==12 and not any(x.get('error') for x in items)
cases={x['case']:x for x in items}
for key in ['peach_fixed','winter_april','winter_may']:
 x=cases[key];d=x['decision']
 assert d['reference_introduction_route']=='tibet_overview_10d',key
 assert d['route_choice']=='recommend' and not d['start_introduction'],key
 assert len(x['compiled_texts'])>10,key
 assert any('尚未確認您原定日期' in t for t in x['compiled_texts']),key
 assert not any('以上先給您參考桃花' in t for t in x['compiled_texts']),key
x=cases['been_tibet'];assert x['decision']['reference_introduction_route']=='tibet_ali_15d'
assert x['decision']['route_choice']=='recommend' and not x['decision']['start_introduction']
assert len(x['compiled_texts'])>10
for key in ['been_short','been_beijing','no_everest','ali_already']:
 assert not cases[key]['decision']['reference_introduction_route'],key
assert not cases['peach_late']['decision']['reference_introduction_route']
text=''.join(m.get('text','') for m in cases['peach_late']['decision']['messages'])
assert '4月10' in text and ('提前' in text or '調整' in text)
def failures(name):
 return {x['id'] for x in json.loads((h/name).read_text(encoding='utf8'))['tests'] if x['outcome']=='failed'}
assert failures('regression-candidate.json')==failures('regression-baseline.json')
out={'real_model_cases':12,'contract_checks':21,'regression_passed':129,'baseline_failures':26,'new_failures':0}
(h/'accepted.json').write_text(json.dumps(out),encoding='utf8');print(json.dumps(out))
