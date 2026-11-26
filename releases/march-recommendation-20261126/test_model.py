import os,sys,json,copy,argparse
from pathlib import Path
from types import SimpleNamespace
ROOT=Path('/opt/china2go-ai');HERE=Path(__file__).resolve().parent
os.chdir(ROOT/'backend');sys.path.insert(0,str(ROOT/'backend'))
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://')
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.reception_v3 import skills,runtime,service
from app.material_library import candidate_materials
from app.automation_models import AutomationSession
parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--baseline',action='store_true');parser.add_argument('--only');args=parser.parse_args()
db=Session(create_engine('sqlite:///file:/opt/china2go-ai/backend/data/app.db?mode=ro&uri=true'));bundle=skills.compile_skills(db);materials=candidate_materials(db,1)
if not args.baseline:
 exec(compile((HERE/'payload/backend/app/reception_v3/runtime.py').read_text(),str(HERE/'payload/backend/app/reception_v3/runtime.py'),'exec'),runtime.__dict__)
 class Registry(skills.SkillRegistry):
  def __init__(self,*a,**kw):
   super().__init__(*a,**kw)
   for m in json.loads((HERE/'manifest.json').read_text()):
    p=HERE/m['payload']
    if p.name=='SKILL.md': self.skills[p.parent.name]['body']=p.read_text().split('---',2)[2]
 runtime.SkillRegistry=Registry
snap=json.loads((ROOT/'releases/tibet-entry-evidence-20261125/before.json').read_text());runs={r['id']:r for c in snap['cases'].values() for r in c['runs']}
def snapshot(r):
 x=runs[r]['input_snapshot'];return json.loads(x) if isinstance(x,str) else copy.deepcopy(x)
base=snapshot(10293);cases=[]
def add(key,text,route='tibet_overview_10d',profile=None,state=None):
 ctx=copy.deepcopy(base);ctx.update(route_variant=route,profile=profile or {},state=state or {'opening_started':True,'intake_status':'done','completed_introductions':[route]},now='2026-11-26T06:00:00+00:00')
 q=copy.deepcopy(base['buffered_questions'][-1]);q.update(content=text,created_at=ctx['now']);ctx['buffered_questions']=[q];ctx['messages']=[dict(q,direction='incoming',status='received')];cases.append((key,ctx))
add('march','我想明年3月去西藏，日期還沒定。')
add('late_march','我們2027年3月底出發，兩位。')
add('early_fixed','只能2027年3月3日出發，不能改日期。')
add('eight_days','2027年3月去西藏，最多只有8天假。')
add('avoid_everest','2027年3月底，不要去珠峰，9天可以。')
add('declined','2027年3月去西藏，但不想賞桃花，也不想換桃花行程。')
add('future_year','2028年3月底想去西藏。')
add('beijing','2027年3月想去北京，請介紹。','beijing_micro_5d')
add('yunnan','2027年3月想去雲南8天，請介紹。','yunnan_tw_8d')
add('ali','2027年3月想去阿里轉山，4位。','tibet_ali_15d')
add('compare','桃花11日和9日我都想看看，2027年3月底。','peach_11d_2027',state={'opening_started':True,'intake_status':'done'})
add('already_seen','我們2027年3月底出發，桃花11天剛才看過了。','tibet_overview_10d',state={'opening_started':True,'intake_status':'done','completed_introductions':['peach_11d_2027','tibet_overview_10d']})
if args.only:cases=[(k,c) for k,c in cases if k in args.only.split(',')]
result={'status':'running','outbound_requests':0,'cases':[]}
for key,ctx in cases:
 ctx.update(skills=bundle,available_materials=materials)
 try:
  decision,logs,_=runtime.run_agent(ctx)
 except Exception as exc:
  result['cases'].append({'id':key,'decision':{},'plan':[],'application_error':str(exc),'contract_repairs':[],'error_logs':getattr(exc,'logs',[]),'error_logs':getattr(exc,'logs',[]),'error_logs':getattr(exc,'logs',[])})
  Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));continue
 row=AutomationSession(id=-991,owner_id=1,inbox_binding_id=ctx.get('inbox_binding_id',1) or 1,environment='playground',engine_version='v3',mode='journey',messages=copy.deepcopy(ctx['messages']),memory=copy.deepcopy(ctx.get('profile',{})),controls={'route_variant':ctx['route_variant'],'v3':copy.deepcopy(ctx['state']),'simulation':{'status':'running'}},virtual_now=ctx['now'])
 run=SimpleNamespace(id=-991,input_snapshot=ctx,trace={'logs':[]})
 try:
  service.apply_decision(db,row,run,bundle,decision)
  plan=[m for m in row.messages if m.get('status')=='draft']
  error=None
 except Exception as exc:plan=[];error=str(exc)
 result['cases'].append({'id':key,'decision':decision,'plan':plan,'application_error':error,'contract_repairs':[l.get('error_code') for l in logs if l.get('error_code')]})
 Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
result['status']='completed';Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({'status':result['status'],'cases':len(cases)}))
