"""Exercise the normal playground executor without sending to real customers."""
import argparse
import hashlib
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import run_advisor_playground_acceptance as qa
from app.automation_service import add_customer_message, gate
from app.web_knowledge import install_curated_global_library, publish_revision, active_web_facts
from app.models import Tenant
from app.release_provenance import source_fingerprint, runtime_config_fingerprint

parser=argparse.ArgumentParser()
parser.add_argument('--publish-knowledge-only',action='store_true')
parser.add_argument('--only', help='Comma-separated scenario keys')
parser.add_argument('--output',default='question-acceptance.json')
args=parser.parse_args()
root=Path(__file__).resolve().parents[2]
with qa.SessionLocal() as db:
    assert not qa.global_message_sending_enabled(db)
    before=qa._outbound_count(db)
    tenant=db.scalar(qa.select(Tenant).order_by(Tenant.id))
    payload=json.loads((root/'data/knowledge/china2go/global-website-knowledge/curated-modules.json').read_text(encoding='utf-8'))
    source,revision=install_curated_global_library(db,payload,tenant_id=tenant.id,user_id=1)
    publish_revision(db,source,revision,runtime_scope='playground')
    db.commit()
    assert active_web_facts(db,tenant.id,'公司 LINE 供氧',environment='live',candidate_pool=True)[0]==[]
    assert active_web_facts(db,tenant.id,'公司 LINE 供氧',environment='playground',candidate_pool=True)[0]
    if args.publish_knowledge_only:
        print(json.dumps({'source_id':source.id,'revision_id':revision.id,'runtime_scope':source.runtime_scope}))
        raise SystemExit(0)
    owner=db.scalar(qa.select(qa.User).where(qa.User.active.is_(True),qa.User.role.in_(['admin','super_admin'])).order_by(qa.User.id))
    inbox=db.scalar(qa.select(qa.InboxBinding).where(qa.InboxBinding.chatwoot_inbox_id==128859))
    owner_id,inbox_id=owner.id,inbox.id
    config_hash=runtime_config_fingerprint(db)
source_hash=source_fingerprint()

SCENARIOS=[
    ('select9',['您好，我想了解行程','桃花9日'], 'reply',2),
    ('select11',['桃花加珠峰11日'],'reply',2),
    ('hotel',['桃花11日住宿是什麼飯店？有房間照片嗎？'],'reply',1),
    ('oxygen',['桃花11日下車走行程也有氧氣嗎？','自己要準備嗎'],'reply',0),
    ('family',['桃花9日，我先跟家人討論，你先給我行程重點。'],'reply',2),
    ('pause',['桃花9日','暫時不用了，之後需要再找你'],'no_action',3),
    ('accidental',['抱歉我按錯了，沒有旅遊計畫'],'no_action',3),
    ('resume',['暫時不用了','我又想了解了，桃花9日的價格多少？'],'reply',1),
    ('discount',['桃花11日兩個人同行有優惠嗎？'],'handoff',0),
    ('company',['請問你們公司在哪裡？有官網嗎？'],'reply',0),
    ('wechat',['可以用微信聯絡嗎？'],'reply',0),
    ('health',['我有心臟病，適合去西藏吗？能保證不會高反嗎？'],'reply',0),
    ('large_group',['我們8個人要去桃花11日'],'handoff',0),
    ('composite',['桃花11日','我想規劃明年四月初，請問二人費用及這個行程的安排活動及景點？'],'reply',0),
    ('mixed_confirmation',['桃花11日團費多少？現在道路通嗎？'],'handoff',0),
    ('transfer',['桃花11日','11480拼房-600早鳥 不含往返交通運費（林芝接、拉薩送）？'],'handoff',0),
    ('unselected_price',['可以先給我行程跟價格天數讓我考慮嗎？'],'reply',0),
    ('permit_flight',['桃花11日','規劃中\n台胞代辧入藏證？到林芝機票自訂？'],'reply',0),
    ('current_conditions_contact',['2月底3月初\n請問最近西藏不是有地震土石流，是在景點附近嗎？\n可以用微信聯繫嗎'],'handoff',0),
]
results=[]
report={'source_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'source_fingerprint':source_hash,
        'runtime_config_fingerprint':config_hash,'sessions':results,'outbound_before':before,'passed':False}

def snapshot(sid):
    with qa.SessionLocal() as db:
        s=db.get(qa.AutomationSession,sid)
        runs=db.scalars(qa.select(qa.AutomationRun).where(qa.AutomationRun.session_id==sid).order_by(qa.AutomationRun.id)).all()
        return {'messages':s.messages,'controls':s.controls,'runs':[
            {'id':r.id,'module':r.module,'status':r.status,
             'decision':{k:v for k,v in (r.decision or {}).items() if k not in {'bound_route_snapshot','materials'}},
             'trace':{k:v for k,v in (r.trace or {}).items() if k in {
                 'question_details','discussion_subject','historical_route_choice','confirmation_questions',
                 'unsupported_claims','unanswered_questions','knowledge_selection','fact_verification_passed',
                 'question_coverage_passed','handoff_task','customer_questions','semantic_signals','knowledge_usage'}}} for r in runs]}

for key,messages,action,touches in SCENARIOS:
    if args.only and key not in args.only.split(','):continue
    item={'key':key,'checks':{},'turns':[],'touches':[]}
    results.append(item)
    try:
        sid=qa._create_session(owner_id,inbox_id,{'key':'real-question:'+key,'title':'真實問答驗收：'+key,'message':messages[0]})
        item['session_id']=sid
        for index,text in enumerate(messages):
            if index:
                with qa.SessionLocal() as db:
                    s=db.get(qa.AutomationSession,sid)
                    add_customer_message(db,s,text,f'question:{sid}:{index}')
                    db.commit()
            qa._wait_ready(sid,drive_worker=True,minimum_outgoing=0,timeout=240)
            turn=snapshot(sid)
            latest=next(r for r in reversed(turn['runs']) if r['module']=='reply')
            item['turns'].append({'input':text,**latest})
            assert latest['status']=='completed',latest
            if key=='select9' and index==0:
                opening_media={i.get('media_id') for i in latest['decision'].get('opening_items',[]) if i.get('media_id')}
                assert not latest['decision'].get('material_keys')
                assert all(m.get('media_id') in opening_media for m in turn['messages'] if m.get('media_id'))
        latest=item['turns'][-1]
        checks=item['checks']
        checks['expected_action']=latest['decision']['action']==action
        if action=='handoff': checks['persisted_task']=bool(snapshot(sid)['controls'].get('handoff_tasks'))
        if action=='reply':checks['nonempty_reply']=bool(latest['decision'].get('reply'))
        if key=='company': checks['official_location_and_url']='上海' in latest['decision'].get('reply','') and 'china2go.com' in latest['decision'].get('reply','')
        if key=='wechat':checks['no_payment_answer']='支付' not in latest['decision'].get('reply','')
        if key=='current_conditions_contact':
            checks['answers_contact_channel']='微信' in latest['decision'].get('reply','')
            checks['known_answer_not_discarded']=latest['decision'].get('handoff_reason')=='knowledge_confirmation_required'
        if key=='oxygen':checks['oxygen_reference']='氧' in latest['decision'].get('reply','') and '證件' not in latest['decision'].get('reply','')
        if key=='oxygen':checks['oxygen_scope']='5000' in latest['decision'].get('reply','').replace(',','')
        if key in {'composite','mixed_confirmation'}:
            checks['answers_known_price']='11480' in latest['decision'].get('reply','').replace(',','')
        if key=='composite':checks['answers_attractions']='珠峰' in latest['decision'].get('reply','')
        if key=='transfer':
            answer=latest['decision'].get('reply','')
            checks['answers_published_transfers']='林芝' in answer and ('拉薩' in answer or '拉萨' in answer)
            checks['answers_known_price']='11480' in answer.replace(',','')
        if key=='unselected_price':
            answer=latest['decision'].get('reply','').replace(',','')
            checks['answers_two_prices']='9980' in answer and '11480' in answer
            checks['unselected_no_images']=not latest['decision'].get('material_keys')
        if key=='permit_flight':
            answer=latest['decision'].get('reply','')
            checks['answers_permit']='入藏' in answer
            checks['answers_flight_service']='機票' in answer and ('代訂' in answer or '自訂' in answer or '自行' in answer)
        if key=='hotel':checks['hotel_asset_selected']=any('hotel' in str(x).lower() or 'hilton' in str(x).lower() or '希爾頓' in str(x) for x in latest['decision'].get('material_keys',[]))
        if action=='no_action':
            count=len(snapshot(sid)['messages'])
            for offset in [10,30,120]:
                with qa.SessionLocal() as db:
                    s=db.get(qa.AutomationSession,sid)
                    s.virtual_now=qa.iso(qa.dt(s.virtual_now)+timedelta(minutes=offset))
                    assert gate(s,s.virtual_now,True,db)=='customer_paused_proactive'
                    qa.advance_sops(db,s)
                    db.commit()
            checks['no_proactive_after_pause']=len(snapshot(sid)['messages'])==count
        else:
            for n in range(touches):
                old={m['id'] for m in snapshot(sid)['messages']}
                jid=qa._advance_next_silence(sid)
                if not jid:
                    item['touches'].append({'status':'no_scheduled_content'})
                    break
                qa._wait_silence_terminal(sid,jid,drive_worker=True,timeout=240)
                snap=snapshot(sid)
                with qa.SessionLocal() as db: status=db.get(qa.RehearsalJob,jid).status
                new=[m for m in snap['messages'] if m['id'] not in old and m.get('direction')=='outgoing' and m.get('status')=='simulated_delivered']
                item['touches'].append({'status':status,'messages':new})
                assert status in {'simulated_delivered','skipped'},status
                if status=='simulated_delivered':assert new
            if touches:checks['silence_checked']=bool(item['touches'])
            if key=='family':checks['family_new_value']=any(t.get('messages') for t in item['touches'])
        snap=snapshot(sid)
        item['messages']=snap['messages']
        checks['no_failed_runs']=all(r['status']=='completed' for r in snap['runs'])
        checks['unique_message_ids']=len({m['id'] for m in snap['messages']})==len(snap['messages'])
        item['passed']=all(checks.values())
    except Exception as exc:
        item['error']=type(exc).__name__+':'+str(exc)
        item['passed']=False
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'key':key,'session_id':item.get('session_id'),'passed':item['passed'],'checks':item['checks'],'error':item.get('error')},ensure_ascii=False),flush=True)

with qa.SessionLocal() as db:
    report['outbound_after']=qa._outbound_count(db)
    assert before==report['outbound_after'] and not qa.global_message_sending_enabled(db)
    assert config_hash==runtime_config_fingerprint(db)
assert source_hash==source_fingerprint()
report['passed']=bool(results) and all(row['passed'] for row in results)
Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
raise SystemExit(0 if report['passed'] else 1)
