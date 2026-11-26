"""Actual model smoke across enabled products, with outbound disabled."""
import os,sys,json,copy
from pathlib import Path
ROOT=Path('/opt/china2go-ai');HERE=Path(__file__).resolve().parent
os.chdir(ROOT/'backend');sys.path.insert(0,str(ROOT/'backend'))
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://')
from app.reception_v3 import runtime,skills,prompt
for name,module in [('prompt',prompt),('skills',skills),('runtime',runtime)]:
 p=HERE/'payload/backend/app/reception_v3'/f'{name}.py';exec(compile(p.read_text(),str(p),'exec'),module.__dict__)
base=json.loads((ROOT/'releases/incident-1437-20261126/input.json').read_text());results=[]
for route in base['skills']['routes']:
 ctx=copy.deepcopy(base);ctx.update(event='customer_message',route_variant=route,profile={},state={'opening_started':True,'intake_status':'done','completed_introductions':[route]})
 q={'id':'test-current','content':'請問這條行程住宿和車子怎麼安排？','direction':'incoming','status':'received','created_at':ctx['now']}
 ctx['messages']=[q];ctx['buffered_questions']=[q];ctx.pop('route_understanding',None)
 try:
  decision,logs,_=runtime.run_agent(ctx);results.append({'route':route,'decision':decision,'logs':logs})
 except Exception as exc:results.append({'route':route,'error':str(exc),'logs':getattr(exc,'logs',[])})
 (HERE/'routes-result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps({'count':len(results),'errors':[x for x in results if x.get('error')]}))
