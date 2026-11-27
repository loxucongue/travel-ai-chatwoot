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

report = {'checks': [], 'model': []}
def check(name, result):
    assert result, name
    report['checks'].append(name)
def save():
    tmp=HERE/'effect.tmp';tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(HERE/'effect.json')
def profile(residence='hong_kong', document='home_return', scope='all'):
    return {docs.KEY: {'residence':residence,'document':document,'scope':scope,'evidence':[{'message_id':'test','quote':'我們全是香港人，用回鄉證'}]}}

check('HK ordinary home return exempt', docs.status(profile())=='exempt')
check('Macao ordinary home return exempt', docs.status(profile('macao'))=='exempt')
check('Taiwan permit required',docs.status(profile('taiwan','taiwan_permit'))=='required')
check('mixed needs review',docs.status(profile(scope='mixed'))=='review')
check('non-Chinese permit needs review',docs.status(profile(document='non_chinese_permit'))=='review')
check('foreign passport needs review',docs.status(profile(document='foreign_passport'))=='review')
check('residence alone insufficient',docs.status(profile(document='unknown'))=='unknown')
check('dialect only asks',docs.task({'route_variant':'peach_11d_2027','messages':[{'direction':'incoming','content':'集合地點會係邊？'}]})['ask_origin'])
check('known residence no repeated question',not docs.task({'route_variant':'peach_11d_2027','messages':[{'direction':'incoming','content':'集合地點係邊？'}],'profile':profile()})['ask_origin'])
check('human asked no repeated question',not docs.task({'route_variant':'peach_11d_2027','messages':[{'direction':'outgoing','status':'sent','content':'請問是香港旅客嗎？','attribution':'human'},{'direction':'incoming','content':'集合係邊？'}]})['ask_origin'])
check('failed question not counted',docs.task({'route_variant':'peach_11d_2027','messages':[{'direction':'outgoing','status':'failed','content':'請問是香港旅客嗎？'},{'direction':'incoming','content':'集合係邊？'}]})['ask_origin'])
check('Beijing not given Tibet questions',not docs.task({'route_variant':'beijing_private','messages':[{'direction':'incoming','content':'酒店喺邊？'}]})['applicable'])
try:
    docs.validate_extraction(profile()[docs.KEY],[{'id':'test','speaker':'assistant','text':'我們全是香港人，用回鄉證'}])
    raise AssertionError('assistant evidence accepted')
except ValueError: report['checks'].append('assistant evidence rejected')
check('no info preserves memory',docs.apply_understanding({'profile':profile()}, {'traveler_documents':{}})['profile']==profile())
check('new explicit correction replaces state',docs.status(docs.apply_understanding({'profile':profile()}, {'traveler_documents':profile(document='foreign_passport')[docs.KEY]})['profile'])=='review')
check('preserve inclusion list',docs.scoped_text('包含：車、入藏函、導遊、住宿。',profile())=='包含：車、導遊、住宿。')
check('preserve exemption answer',docs.scoped_text('您持回鄉證不需要入藏函。',profile())=='您持回鄉證不需要入藏函。')
check('remove Chengdu clause only',docs.scoped_text('第一天林芝集合。入藏函在成都交給您。',profile())=='第一天林芝集合。')
check('unknown inclusion qualified',docs.scoped_text('包含：入藏函、車、住宿。',{})=='包含：入藏函（按旅客證件適用）、車、住宿。')
check('no automatic exemption from dialect',docs.status({})=='unknown')
check('no proactive border explanation',bool(docs.reply_error({'action':'reply','messages':[{'text':'邊防證我們會協助辦理。'}]}, {'route_variant':'peach_11d_2027','messages':[{'direction':'incoming','content':'集合在哪？'}]})))
check('border question answered',not docs.reply_error({'action':'reply','messages':[{'text':'邊防證我們會協助辦理。'}]}, {'route_variant':'peach_11d_2027','messages':[{'direction':'incoming','content':'珠峰要邊防證嗎？'}]}))
check('private border handoff reason allowed',not docs.reply_error({'action':'handoff','handoff_reason':'顧問核對邊防證','messages':[]}, {'route_variant':'peach_11d_2027','messages':[{'direction':'incoming','content':'幫我確認證件。'}]}))
bundle=base['skills'];before=copy.deepcopy(bundle)
scoped=docs.scoped_bundle(bundle,profile())
check('bundle never mutated',before==bundle)
for route, spec in bundle['routes'].items():
    if not docs.is_tibet(route,spec):check('other route preserved '+route,scoped['routes'][route]==spec)
route='tibet_winter_8d'
now='2026-12-01T10:00:00+08:00';day='2026-12-17'
for label, value in [('hk',profile()),('macao',profile('macao')),('taiwan',profile('taiwan','taiwan_permit')),('unknown',{}),('mixed',profile(scope='mixed'))]:
    result=seasonal_quotes.departure_quote(bundle,route,day,day,now,traveler_profile=value)
    report.setdefault('date_checks',{})[label]=result
    if label in ('hk','macao'):check('no permit deadline '+label,result.get('status') not in ('advisor','ask_document') and result.get('traveler_permit_status')=='exempt')
    if label=='taiwan':check('Taiwan deadline retained',result['status']=='advisor')
    if label=='unknown':check('unknown asks document',result['status']=='ask_document')
save()

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.automation_models import AutomationSession
db=Session(create_engine('sqlite:///file:/opt/china2go-ai/backend/data/app.db?mode=ro&uri=true'))
cases=[
    ('dialect','集合地點會係邊？',{}),
    ('hk','我們兩位都是香港旅客，用普通回鄉證。集合地點會係邊？',{}),
    ('macao','我一個人，澳門居民，用回鄉證，入藏函在哪裡領？',{}),
    ('non_chinese','我是香港永久居民，持非中國籍港澳通行證，可以不用入藏函嗎？',{}),
    ('mixed','我用香港回鄉證，同行朋友是台灣人用台胞證，我們都不用入藏函嗎？',{}),
    ('correction','更正一下，我今次用英國護照，不是回鄉證。',profile()),
    ('known','集合地點在哪？',profile()),
    ('border','我們都持香港回鄉證，去珠峰還需要辦邊防證嗎？',{}),
    ('taiwan_dialect','我係台灣人，用台胞證，集合喺邊？',{}),
    ('short_hk','我們兩位都是香港人，用普通回鄉證，想12月17日出發，可以嗎？',{}),
    ('short_taiwan','我們兩位都是台灣人，用台胞證，想12月17日出發，可以嗎？',{}),
    ('original_1446','',{}),
]
for index,(label,text,known) in enumerate(cases):
    ctx=copy.deepcopy(base);ctx['route_variant']='peach_11d_2027';ctx['profile']=known
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
