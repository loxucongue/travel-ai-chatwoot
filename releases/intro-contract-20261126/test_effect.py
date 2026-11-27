"""Read-only historical model replays and date/source boundary checks."""
import os,sys,json,copy,sqlite3
from pathlib import Path
HERE=Path(__file__).resolve().parent
os.chdir('/opt/china2go-ai/backend');sys.path.insert(0,os.getcwd())
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://')
from app.reception_v3 import reference_introduction,runtime
for name,module in [('reference_introduction',reference_introduction),('runtime',runtime)]:
 p=HERE/'payload/backend/app/reception_v3'/f'{name}.py'
 exec(compile(p.read_text(encoding='utf-8'),str(p),'exec'),module.__dict__)
with sqlite3.connect('file:data/app.db?mode=ro',uri=True) as db:
 rows={rid:json.loads(db.execute('select input_snapshot from automation_runs where id=?',(rid,)).fetchone()[0]) for rid in (11353,)}
report={'model':[]}
for repeat in range(3):
 try:
  decision,logs,_=runtime.run_agent(copy.deepcopy(rows[11353]));item={'run':11353,'repeat':repeat,'decision':decision,'logs':logs}
 except Exception as e:item={'run':11353,'repeat':repeat,'error':str(e),'logs':getattr(e,'logs',[])}
 report['model'].append(item)
 tmp=HERE/'effect.tmp';tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(HERE/'effect.json')
print('complete',flush=True)
