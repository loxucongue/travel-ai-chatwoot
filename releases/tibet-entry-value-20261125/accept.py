import json,hashlib,re,sys
from pathlib import Path
p=Path(__file__).resolve().parent;r=json.loads(Path(sys.argv[1]).read_text());assert r['status']=='completed'
cases={c['id']:c for c in r['cases']};assert len(cases)==12
assert all(c['application_error'] is None for c in cases.values())
def text(k):return '\n'.join(m.get('content','') for m in cases[k]['plan'])
for k in ['replay_9999','replay_10002','early_entry']:
 t=text(k);assert '入藏' in t and any(s in t for s in ['不能','沒辦法','不可以','不建議']), (k,t)
 assert any(s in t for s in ['4位','四位','4人','四人']),(k,t)
 assert cases[k]['decision']['profile'].get('group_type')!='private'
 assert cases[k]['decision']['profile'].get('party_size')!=4,'offered four-person option must not overwrite actual two-to-three people'
t=text('four');assert '申請' in t and cases['four']['decision']['profile'].get('group_type')!='private',t
assert '自己一團' not in t and '獨立成團' not in t,t
assert '成都' in text('chengdu') and any(s in text('chengdu') for s in ['可以','沒問題'])
for k in ['replay_1291','winter','ali']:
 plan=cases[k]['plan'];quotes=[i for i,m in enumerate(plan) if m.get('post_quote_minutes')]
 assert quotes,(k,plan)
 images=[(i,m['asset_key']) for i,m in enumerate(plan) if m.get('asset_key')]
 assert len(images)>=2 and all(i<quotes[0] for i,_ in images),(k,images,quotes)
 assert all(m.get('post_quote_minutes')==5 for m in plan if m.get('post_quote_minutes'))
 assert any('車' in m.get('content','') for m in plan[:quotes[0]])
 assert any(any(s in m.get('content','') for s in ['酒店','住宿','飯店']) for m in plan[:quotes[0]])
for k in ['direct_price','already_seen']:
 assert not any(m.get('asset_key') for m in cases[k]['plan'])
 assert any(m.get('post_quote_minutes')==5 for m in cases[k]['plan'])
assert '7,580' in text('yunnan') or '7580' in text('yunnan')
assert '北京' in text('beijing')
out={'status':'passed','manifest_sha256':hashlib.sha256((p/'manifest.json').read_bytes()).hexdigest(),'cases':list(cases),'plan_order_checked':['replay_1291','winter','ali'],'outbound_requests':0,'contract_repair_cases':[k for k,v in cases.items() if v['contract_repairs']]}
Path(sys.argv[2]).write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False))
