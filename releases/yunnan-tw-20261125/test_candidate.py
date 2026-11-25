"""Isolated production-module checks; never invokes a customer send endpoint."""
import os,sys,json,copy,hashlib,argparse
from pathlib import Path
from types import SimpleNamespace
ROOT=Path('/opt/china2go-ai');PAYLOAD=Path(__file__).resolve().parent;ROUTE='yunnan_tw_8d'
os.chdir(ROOT/'backend');sys.path.insert(0,str(ROOT/'backend'))
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://')
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session
from app.main import app
from app.db import Base
from app.models import Tenant,User,KnowledgeVersion,MaterialAsset,InboxBinding
from app import route_packages as rp
from app.reception_v3 import skills,runtime,service
from app.reception_config import get_reception_configuration
from app.material_library import candidate_materials
from scripts.stage_tibet_routes_20261014 import install_materials
from app.automation_models import AutomationSession
from app.reception_v3.large_party import policy_for,undelivered_parts
from app.reception_v3.route_followup import policy_for as followup_policy

def setup():
 candidate=rp._validate(json.loads((PAYLOAD/'route-package.json').read_text()),PAYLOAD/'route-package.json')
 original=rp.load_route_packages;packages=copy.deepcopy(original());packages[ROUTE]=candidate
 class Loader:
  def __call__(self):return packages
  def cache_clear(self):pass
 rp.load_route_packages=Loader();rp.reload_route_packages()
 with Session(create_engine('sqlite:///file:/opt/china2go-ai/backend/data/app.db?mode=ro&uri=true')) as db:
  config=get_reception_configuration(db);baseline=skills.compile_skills(db)
 changed=copy.deepcopy(config);changed['routing']['enabled_route_variants'].append(ROUTE)
 skills.get_reception_configuration=lambda db:changed
 engine=create_engine('sqlite://');Base.metadata.create_all(engine);db=Session(engine)
 db.add(Tenant(id=1,name='isolated-yunnan'));db.add(User(id=1,email='isolated@example.invalid',display_name='isolated',password_hash='unused',role='super_admin'));db.add(KnowledgeVersion(id=1,tenant_id=1,version_key=candidate['knowledge_version'],title='isolated',content_hash='test',source_summary={}));db.flush()
 db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=-1,name='isolated'));db.flush()
 manifest=json.loads((PAYLOAD/'asset-manifest.json').read_text());active=[a for a in manifest if a['available']]
 files={a['asset_key']:(PAYLOAD/'media'/(a['sha256']+'.jpg')).read_bytes() for a in active}
 # Hash-addressed candidate files are immutable; install helper uses a temporary media directory.
 import tempfile
 temp=Path(tempfile.mkdtemp(prefix='yunnan-test-'))
 install_materials(db,active,files,temp,1,1,1)
 bundle=skills.compile_skills(db);materials=candidate_materials(db,1)
 assert all(bundle['routes'][k]==baseline['routes'][k] for k in baseline['routes'])
 class Registry(skills.SkillRegistry):
  def __init__(self,*a,**kw):
   super().__init__(*a,**kw)
   text=(PAYLOAD/'SKILL.md').read_text().split('---',2);import yaml;meta=yaml.safe_load(text[1])
   self.skills['yunnan-tw-8d']={'name':meta['name'],'description':meta['description'],'body':text[2]}
 runtime.SkillRegistry=Registry
 return db,bundle,materials,Registry,manifest,temp

def structural(db,bundle,materials,Registry,manifest,temp):
 results=[]
 assert len(manifest)==50 and len(materials)==49
 assert len({a['sha256'] for a in manifest})==49
 assert all(hashlib.sha256((PAYLOAD/'media'/(a['sha256']+'.jpg')).read_bytes()).hexdigest()==a['sha256'] for a in manifest)
 assert all(Registry(bundle).route_skill(r) for r in bundle['routes'])
 assert bundle['lead_capture']['post_quote_invitation_minutes']==5
 for n in [1,2,3,4,5,6,7,8]:
  profile={'party_size':n}
  assert bool(policy_for(bundle,ROUTE,profile))==(n>=7)
  row=AutomationSession(id=-n,owner_id=1,inbox_binding_id=1,environment='playground',engine_version='v3',mode='journey',messages=[],memory=profile,controls={'route_variant':ROUTE,'v3':{'opening_started':True},'simulation':{'status':'running'}},virtual_now='2026-11-25T13:00:00+00:00')
  context={'route_variant':ROUTE,'profile':profile,'skills':bundle,'messages':[],'now':row.virtual_now,'state':row.controls['v3'],'available_materials':materials}
  run=SimpleNamespace(id=-n,input_snapshot=context,trace={'logs':[]})
  service.apply_decision(db,row,run,bundle,runtime.Decision(action='reply',route_variant=ROUTE,route_choice='selected',start_introduction=True).model_dump())
  plan=[m for m in row.messages if m.get('direction')=='outgoing']
  assert plan and not any(m.get('asset_key')==ROUTE+'_yn_1' for m in plan)
  assert not any('入藏' in (m.get('content') or '') or '西藏' in (m.get('content') or '') for m in plan)
  results.append({'party':n,'parts':len(plan),'handoff_after_plan':bool(row.controls.get('v3',{}).get('handoff_after_plan'))})
 policy=followup_policy(bundle,ROUTE,{'party_size':2})
 assert policy['checkpoints']=={'intro_wait':30,'quote_wait':5,'daily_wait':1440}
 repeated={'content':'已發文字','asset_key':'same','media_hash':'samehash'}
 row=SimpleNamespace(messages=[dict(repeated,status='delivered',direction='outgoing')])
 assert undelivered_parts(row,[repeated,{'content':'新文字'}])==[{'content':'新文字'}]
 now='2026-11-25T13:00:00+00:00'
 def fresh(profile=None):
  return AutomationSession(id=-90,owner_id=1,inbox_binding_id=1,environment='playground',messages=[],memory=profile or {},controls={'route_variant':ROUTE,'v3':{'opening_started':True},'simulation':{'status':'running','last_wall_at':now}},virtual_now=now)
 row=fresh();value=service.state(row)
 assert service.begin_intake(row,value,bundle,ROUTE)
 service.save(row,value);part=row.messages[-1];service.complete_delivery(row,part['id'],'simulated_delivered')
 service.advance(row,service.later(now,29));assert service.state(row)['intake_status']=='waiting'
 row.controls=json.loads(json.dumps(row.controls));row.messages=json.loads(json.dumps(row.messages))
 service.advance(row,service.later(now,30));assert service.state(row)['pending_event']=='intake_due'
 known=fresh({'party_size':2});assert not service.begin_intake(known,service.state(known),bundle,ROUTE)
 quote=fresh({'party_size':2,'departure_window':'2027年3月'})
 run=SimpleNamespace(id=-90,input_snapshot={'event':'customer_message','profile':quote.memory,'skills':bundle,'messages':[],'now':now,'available_materials':materials},trace={'logs':[]})
 dec=runtime.Decision(route_variant=ROUTE,messages=[{'text':'目前3月參考團費每人人民幣7,580，2人一間。','is_trip_quote':True}],followup_checkpoint='quote_wait',next_check_minutes=5).model_dump()
 service.apply_decision(db,quote,run,bundle,dec)
 for m in list(quote.messages):
  if m.get('status')=='draft':service.complete_delivery(quote,m['id'],'simulated_delivered')
 value=service.state(quote);assert value.get('post_quote_pending'),value
 service.advance(quote,service.later(now,299));assert service.state(quote).get('pending_event')!='silence_due'
 service.advance(quote,service.later(now,300));assert service.state(quote).get('pending_event')=='silence_due'
 return {'status':'passed','plans':results,'enabled_routes':list(bundle['routes']),'active_assets':len(materials),'all_existing_routes_unchanged':True,'virtual_checks':['intake 29/30 seconds','intake persisted across JSON reload','known party not asked again','quote 299/300 seconds','material hash/text dedup'],'outbound_requests':0}

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--cases');parser.add_argument('--output',required=True);args=parser.parse_args()
 db,bundle,materials,Registry,manifest,temp=setup()
 try:
  result=structural(db,bundle,materials,Registry,manifest,temp)
  if args.cases:
   import sqlite3
   with sqlite3.connect('file:/opt/china2go-ai/backend/data/app.db?mode=ro',uri=True) as d:raw=d.execute('select input_snapshot from automation_runs where id=11117').fetchone()[0]
   template=json.loads(raw);results=[]
   for case in json.loads(Path(args.cases).read_text()):
    ctx=copy.deepcopy(template);ctx.update(skills=bundle,available_materials=materials,route_variant=case.get('route',ROUTE),profile=case.get('profile',{}),state=case.get('state',{'opening_started':True,'introduction_complete':True}),now='2026-11-25T13:00:00+00:00',event=case.get('event','customer_message'))
    q=dict(template['buffered_questions'][-1],content=case['text']);ctx['buffered_questions']=[q];ctx['messages']=[dict(q,direction='incoming',status='received')]
    ctx['messages']=case.get('history',[])+ctx['messages']
    dec,logs,_=runtime.run_agent(ctx)
    record={'id':case['id'],'question':case['text'],'decision':dec,'errors':[l.get('error_code') for l in logs if l.get('error_code')]};results.append(record)
    Path(args.output).write_text(json.dumps({**result,'model_results':results},ensure_ascii=False,indent=2))
   result['model_results']=results
  Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({'status':'completed','output':args.output}))
 finally:
  db.close();import shutil;shutil.rmtree(temp)
