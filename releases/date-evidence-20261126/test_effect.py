"""Read-only historical model replays and date/source boundary checks."""
import os,sys,json,copy,sqlite3
from pathlib import Path
HERE=Path(__file__).resolve().parent
os.chdir('/opt/china2go-ai/backend');sys.path.insert(0,os.getcwd())
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',DATABASE_URL='sqlite://')
from app.reception_v3 import consultation,route_understanding,seasonal_quotes,runtime
for name,module in [('consultation',consultation),('route_understanding',route_understanding),('seasonal_quotes',seasonal_quotes),('runtime',runtime)]:
 p=HERE/'payload/backend/app/reception_v3'/f'{name}.py'
 exec(compile(p.read_text(encoding='utf-8'),str(p),'exec'),module.__dict__)
with sqlite3.connect('file:data/app.db?mode=ro',uri=True) as db:
 rows={rid:json.loads(db.execute('select input_snapshot from automation_runs where id=?',(rid,)).fetchone()[0]) for rid in (10192,10766,11339,11346)}
bundle=rows[10766]['skills'];routes=bundle['routes'];checks=[]
def check(name,value):
 assert value,name
 checks.append(name)
check('ad title',consultation.product_title_only('3~4月｜西藏桃花節＋珠峰 11日',routes))
for text in ('3月','我們3月出發','3~4月｜西藏桃花節＋珠峰 11日，我們4月2日出發','我們想去桃花11日，只有九天假期'):
 check('preserve customer: '+text,not consultation.product_title_only(text,routes))
old=copy.deepcopy(rows[10192]);clean=consultation.verified_date_context(old)
check('no mutation',old==rows[10192])
check('stale title date removed',not clean['profile']['_route_dialogue']['travel_time'].get('start'))
check('route preserved',clean['route_variant']==old['route_variant'])
remembered=copy.deepcopy(old);remembered['profile']['departure_window']='2027-05-08'
check('do not clear unrelated older date',consultation.verified_date_context(remembered)['profile']['departure_window']=='2027-05-08')
mixed=copy.deepcopy(old);mixed['messages'].append({'id':'actual','direction':'incoming','content':'我們4月2日出發'})
mixed['profile']['departure_window']='4月2日'
check('later actual date preserved',consultation.verified_date_context(mixed)['profile']['departure_window']=='4月2日')
dec={'profile':{},'need_evidence':[{'topic':'departure','message_id':'cw-964749329','quote':'3~4月｜西藏桃花節＋珠峰 11日'}]}
check('title cannot become need',bool(consultation.validation_error(dec,old)))
now=rows[10766]['now'];route='tibet_overview_10d'
partial=seasonal_quotes.departure_quote(bundle,route,'2027-03-01','2027-03-31',now)
check('partial range explicit',partial.get('reason_code')=='partial_window')
check('partial has actual dates',bool(partial.get('departure_examples')))
check('examples stay inside open March segment',all('2027-03-01'<=d<='2027-03-10' for d in partial['departure_examples']))
for day in partial['departure_examples']:
 result=seasonal_quotes.departure_quote(bundle,route,day,day,now)
 check('single day revalidated '+day,result.get('status') not in ('ask_date','unavailable','advisor'))
closed=seasonal_quotes.departure_quote(bundle,route,'2027-03-11','2027-03-31',now)
check('closed segment remains closed',not closed.get('departure_examples') and closed.get('reason_code')!='partial_window')
report={'checks':checks,'partial':partial,'closed':closed,'model':[]}
def save():
 tmp=HERE/'effect.tmp';tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(HERE/'effect.json')
save()
for repeat in range(3):
 for rid in (10192,10766,11339,11346):
  try:
   decision,logs,_=runtime.run_agent(copy.deepcopy(rows[rid]));item={'run':rid,'repeat':repeat,'decision':decision,'logs':logs}
  except Exception as e:item={'run':rid,'repeat':repeat,'error':str(e),'logs':getattr(e,'logs',[])}
  report['model'].append(item);save()
print('complete',flush=True)
