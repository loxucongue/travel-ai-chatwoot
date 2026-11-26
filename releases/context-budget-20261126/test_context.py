"""Read-only historical replay; capture requests before transport or call model only."""
import os, sys, json, copy, sqlite3, argparse
from pathlib import Path
ROOT=Path('/opt/china2go-ai'); HERE=Path(__file__).resolve().parent
os.chdir(ROOT/'backend');sys.path.insert(0,str(ROOT/'backend'))
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://')
from app.reception_v3 import runtime, skills, prompt
parser=argparse.ArgumentParser();parser.add_argument('--model',action='store_true');parser.add_argument('--output',required=True);args=parser.parse_args()
baseline_run=runtime.run_agent
original_transport=runtime._post_with_deadline
rows={}
with sqlite3.connect('file:data/app.db?mode=ro',uri=True) as db:
 for rid in (10192,10680,10766,11339,11346):
  row=db.execute('select input_snapshot from automation_runs where id=?',(rid,)).fetchone()
  rows[str(rid)]=json.loads(row[0])
class Captured(BaseException):pass
def capture(ctx):
 result={}
 original_interpret=runtime.interpret
 runtime.interpret=lambda *a,**k:(ctx.get('route_understanding'),[],None)
 def trace(frame,event,arg):
  if event=='line' and frame.f_code.co_name=='run_agent' and 'text_only' in frame.f_locals:
   loc=frame.f_locals
   result.update(characters=len(json.dumps(loc['text_only'],ensure_ascii=False)),loaded=list(loc['loaded']))
   raise Captured()
  return trace
 def forbidden(*a,**k):raise RuntimeError('unexpected_transport')
 runtime._post_with_deadline=forbidden
 sys.settrace(trace)
 try:runtime.run_agent(copy.deepcopy(ctx))
 except Captured:pass
 except Exception as exc:result['error']=str(exc)
 finally:sys.settrace(None);runtime._post_with_deadline=original_transport;runtime.interpret=original_interpret
 return result
out={'before':{k:capture(v) for k,v in rows.items()},'after':{},'model':[]}
for name,module in [('prompt',prompt),('skills',skills),('runtime',runtime)]:
 p=HERE/'payload/backend/app/reception_v3'/f'{name}.py'
 exec(compile(p.read_text(),str(p),'exec'),module.__dict__)
out['after']={k:capture(v) for k,v in rows.items()}
# Every loaded route retains exact fact text in its compact view, and full
# material/reference access remains available. Never abbreviate business facts.
ctx=rows['11346'];registry=skills.SkillRegistry(ctx['skills'],ctx['available_materials'],profile=ctx.get('profile'))
checks=[]
for key,route in ctx['skills']['routes'].items():
 name=registry.route_skill(key); full=registry.load(name); small=registry.load(name,compact=True)
 assert len(small['instructions'])<len(full['instructions'])
 for fact in route.get('facts',[]):assert fact.get('text','') in small['instructions']
 assert '## 可用素材' in full['instructions'] and '## 可用素材' not in small['instructions']
 checks.append(key)
out['compact_fact_checks']=checks
out['all_route_requests']={}
for route in ctx['skills']['routes']:
 for event in ('customer_message','silence_due'):
  sample=copy.deepcopy(ctx);sample.update(route_variant=route,event=event,profile={})
  sample['state']={'opening_started':True,'intake_status':'done','completed_introductions':[route]}
  measured=capture(sample);out['all_route_requests'][route+':'+event]=measured
  assert measured.get('characters',0)<runtime.settings.deepseek_max_input_characters,measured
assert out['before']['11346']['characters']>runtime.settings.deepseek_max_input_characters
assert out['after']['11346']['characters']<runtime.settings.deepseek_max_input_characters
Path(args.output).write_text(json.dumps(out,ensure_ascii=False,indent=2))
if args.model:
 for rid in ('11346','11339','10192','10680','10766'):
  try:
   decision,logs,_=runtime.run_agent(copy.deepcopy(rows[rid]))
   out['model'].append({'run':rid,'decision':decision,'logs':logs})
  except Exception as exc:out['model'].append({'run':rid,'error':str(exc),'logs':getattr(exc,'logs',[])})
  Path(args.output).write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps({'before':out['before'],'after':out['after'],'facts_routes':checks,'model_results':[{k:v for k,v in x.items() if k not in ('logs','decision')} for x in out['model']]},ensure_ascii=False))
