"""Validate actual model decisions and generated delivery plans, not prose alone."""
import json,sys,re
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'));r=r.get('result',r)
assert r['status']=='completed'
cases={c['id']:c for c in r['cases']};checks=[]
assert len(cases)==12
for key,c in cases.items():
 assert c['application_error'] is None,(key,c['application_error'])
 d=c['decision'];p=c['plan']
 assert 'reference_introduction_route' not in d['profile'],key
 if key in ('march','late_march'):
  assert d['route_choice']=='recommend' and d['profile']['discussion_route']=='peach_11d_2027',key
  assert d['route_variant'] in ('','tibet_overview_10d') and not d['start_introduction'],key
  assert d['reference_introduction_route']=='peach_11d_2027',key
  assert any(m.get('reference_introduction_route')=='peach_11d_2027' for m in p),key
  assert not d['followup_checkpoint'],key
  introduction=''.join(m['text'] for m in d['messages'])
  assert len(introduction)<=160,(key,'verbose_recommendation')
  assert not re.search(r'3[/月]11|4[/月]30|每週|每周',introduction),(key,'unnecessary_calendar')
 if key in ('early_fixed','eight_days','declined','future_year','beijing','yunnan','ali'):
  assert not d['reference_introduction_route'],key
  assert d['profile'].get('discussion_route') not in ('peach_11d_2027','peach_9d_2027'),key
 if key=='avoid_everest':
  assert not d['reference_introduction_route'],key
  assert d['profile'].get('discussion_route')=='peach_9d_2027' or d['route_variant']=='peach_9d_2027',key
  assert p,key
 if key=='already_seen':
  assert not d['reference_introduction_route'],key
  assert not any(m.get('reference_introduction_route') for m in p),key
 if key=='compare':
  assert p and (d['reference_introduction_route']=='peach_11d_2027' or d['start_introduction']),key
  assert not any('想上珠峰' in m['text'] or '想不想' in m['text'] for m in d['messages']),key
 checks.append({'case':key,'passed':True,'draft_parts':len(p),'contract_repairs':c['contract_repairs']})
print(json.dumps({'passed':len(checks),'checks':checks},ensure_ascii=False,indent=2))
