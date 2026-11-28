"""Read-only real-model and compiled-plan acceptance; never sends to Chatwoot."""
import os, sys, json, copy, sqlite3, importlib.util
from pathlib import Path
from types import SimpleNamespace
HERE = Path(__file__).resolve().parent
os.chdir('/opt/china2go-ai/backend'); sys.path.insert(0, os.getcwd())
os.environ.update(APP_PROFILE='evaluation', OUTBOUND_MODE='disabled', CHATWOOT_WRITE_ENABLED='false', LIVE_SOP_ENABLED='false', DATABASE_URL='sqlite://')
root = HERE / 'payload/backend/app/reception_v3'
import app.reception_v3 as package
package.__path__.insert(0, str(root))
from app.reception_v3 import traveler_documents as docs
from app.reception_v3 import consultation, route_understanding, seasonal_quotes, reference_introduction, runtime, service
with sqlite3.connect('file:data/app.db?mode=ro', uri=True) as db:
    base = json.loads(db.execute('select input_snapshot from automation_runs where id=11353').fetchone()[0])

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.automation_models import AutomationSession
db=Session(create_engine('sqlite:///file:/opt/china2go-ai/backend/data/app.db?mode=ro&uri=true'))
report={'model':[]}
def save(): (HERE/'effect.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
cases=json.loads((HERE/'cases.json').read_text(encoding='utf8'))
for index,case in enumerate(cases):
    label,text,known=case["name"],case["text"],case.get("profile",{})
    ctx=copy.deepcopy(base);ctx['route_variant']=case.get('route','peach_11d_2027');ctx['profile']=known
    ctx['state']={'completed_introductions':['peach_11d_2027'],'opening_started':True}
    ctx['messages']=[{'id':'fixture-'+label,'direction':'incoming','content':text,'content_type':'text','created_at':ctx['now']}]
    ctx['buffered_questions']=copy.deepcopy(ctx['messages'])
    if label.startswith('short_'):
        ctx['route_variant']='tibet_winter_8d';ctx['now']='2026-12-01T10:00:00+08:00'
        ctx['state']['completed_introductions']=['tibet_winter_8d']
        ctx['messages'][0]['created_at']=ctx['now'];ctx['buffered_questions']=copy.deepcopy(ctx['messages'])
    if label=='original_1446':ctx=copy.deepcopy(base)
    try:
        decision,logs,_=runtime.run_agent(copy.deepcopy(ctx))
        row=AutomationSession(id=-991-index,generation=0,owner_id=1,inbox_binding_id=ctx.get('inbox_binding_id') or 1,environment='playground',engine_version='v3',mode='journey',messages=copy.deepcopy(ctx['messages']),memory=copy.deepcopy(ctx['profile']),controls={'route_variant':ctx['route_variant'],'v3':copy.deepcopy(ctx['state']),'simulation':{'status':'running'}},virtual_now=ctx['now'])
        run=SimpleNamespace(id=-991-index,input_snapshot=ctx,trace={'logs':[]})
        service.apply_decision(db,row,run,ctx['skills'],decision)
        drafts=[m for m in row.messages if m.get('status')=='draft']
        item={'case':label,'decision':decision,'logs':logs,'compiled_texts':[m.get('content','') for m in drafts],'status':docs.status(row.memory)}
    except Exception as exc:item={'case':label,'error':str(exc),'logs':getattr(exc,'logs',[])}
    report['model'].append(item);save()
print('complete',flush=True)
