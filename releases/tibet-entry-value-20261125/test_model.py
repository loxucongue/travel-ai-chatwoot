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
parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--baseline',action='store_true');args=parser.parse_args()
db=Session(create_engine('sqlite:///file:/opt/china2go-ai/backend/data/app.db?mode=ro&uri=true'));bundle=skills.compile_skills(db);materials=candidate_materials(db,1)
if not args.baseline:
 class Registry(skills.SkillRegistry):
  def __init__(self,*a,**kw):
   super().__init__(*a,**kw)
   for m in json.loads((HERE/'manifest.json').read_text()):
    p=HERE/m['payload'];name=p.parent.name;self.skills[name]['body']=p.read_text().split('---',2)[2]
 runtime.SkillRegistry=Registry
snap=json.loads((ROOT/'releases/tibet-entry-evidence-20261125/before.json').read_text());runs={r['id']:r for c in snap['cases'].values() for r in c['runs']}
def snapshot(r):
 x=runs[r]['input_snapshot'];return json.loads(x) if isinstance(x,str) else copy.deepcopy(x)
base=snapshot(10293);cases=[]
for rid in [9999,10002]:cases.append((f'replay_{rid}',snapshot(rid)))
cases.append(('replay_1291',copy.deepcopy(base)))
def add(key,text,route='peach_11d_2027',profile=None,history=None,state=None):
 ctx=copy.deepcopy(base);ctx.update(route_variant=route,profile=profile or {},state=state or {'opening_started':True,'intake_status':'done','completed_introductions':[route]},now='2026-11-25T14:00:00+00:00')
 q=copy.deepcopy(base['buffered_questions'][-1]);q.update(content=text,created_at=ctx['now']);ctx['buffered_questions']=[q];ctx['messages']=(history or [])+[dict(q,direction='incoming',status='received')];cases.append((key,ctx))
add('early_entry','我們2到3位，能4月1日先到林芝住一晚，4月2日再跟團嗎？',profile={'party_size_min':2,'party_size_max':3,'departure_window':'2027年4月1日'})
add('chengdu','可以先到成都住一晚，隔天按團期飛林芝嗎？',profile={'party_size':2})
add('four','我們4位，可以4月1日另開桃花團嗎？',profile={'party_size':4,'departure_window':'2027年4月1日'})
add('direct_price','不用再介紹車和酒店了，只想知道大概價格。','tibet_overview_10d')
add('already_seen','請問這個行程費用？','tibet_overview_10d',history=[{'id':'vehicle-done','direction':'outgoing','status':'delivered','content':'這條用9座VIP商務車，車上有供氧設備。'},{'id':'hotel-done','direction':'outgoing','status':'delivered','content':'酒店安排已介紹：本線住宿為希爾頓，波密和珠峰住宿例外，依實際行程配置。'}])
add('winter','這條冬遊冰川8天大概多少錢？','tibet_winter_8d',state={'opening_started':True,'intake_status':'done'})
add('ali','阿里15天費用多少？','tibet_ali_15d',profile={'party_size':4},state={'opening_started':True,'intake_status':'done'})
add('yunnan','我們2位3月去雲南8天，費用多少？','yunnan_tw_8d',profile={'party_size':2,'departure_window':'2027年3月'})
add('beijing','北京5天住什麼酒店？','beijing_micro_5d')
result={'status':'running','outbound_requests':0,'cases':[]}
for key,ctx in cases:
 ctx.update(skills=bundle,available_materials=materials)
 decision,logs,_=runtime.run_agent(ctx)
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
