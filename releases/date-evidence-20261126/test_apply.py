import os,sys,json,copy,sqlite3
from pathlib import Path
from types import SimpleNamespace
r=Path('/opt/china2go-ai');e=r/'releases/date-evaluation-20261126-v3';os.chdir(r/'backend');sys.path.insert(0,str(r/'backend'))
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://')
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.reception_v3 import service, consultation, route_understanding, seasonal_quotes, runtime
for name,module in [('consultation',consultation),('route_understanding',route_understanding),('seasonal_quotes',seasonal_quotes),('runtime',runtime)]:
 p=e/'payload/backend/app/reception_v3'/f'{name}.py';exec(compile(p.read_text(encoding='utf-8'),str(p),'exec'),module.__dict__)
from app.automation_models import AutomationSession
db=Session(create_engine('sqlite:///file:/opt/china2go-ai/backend/data/app.db?mode=ro&uri=true'))
results=json.loads((e/'effect.json').read_text())['model'];out=[]
for item in results:
 if 'decision' not in item:continue
 with sqlite3.connect('file:data/app.db?mode=ro',uri=True) as conn:ctx=json.loads(conn.execute('select input_snapshot from automation_runs where id=?',(item['run'],)).fetchone()[0])
 row=AutomationSession(id=-991,owner_id=1,inbox_binding_id=ctx.get('inbox_binding_id') or 1,environment='playground',engine_version='v3',mode='journey',messages=copy.deepcopy(ctx['messages']),memory=copy.deepcopy(ctx.get('profile',{})),controls={'route_variant':ctx['route_variant'],'v3':copy.deepcopy(ctx['state']),'simulation':{'status':'running'}},virtual_now=ctx['now'])
 run=SimpleNamespace(id=-991,input_snapshot=ctx,trace={'logs':[]})
 try:
  service.apply_decision(db,row,run,ctx['skills'],item['decision'])
  out.append({'run':item['run'],'status':'passed','draft_count':len([m for m in row.messages if m.get('status')=='draft']), 'quote_texts':[m.get('content','') for m in row.messages if m.get('status')=='draft' and ('10,780' in m.get('content','') or '12,780' in m.get('content',''))]})
 except Exception as exc:out.append({'run':item['run'],'error':str(exc)})
(e/'apply-results.json').write_text(json.dumps(out));print(json.dumps(out))
