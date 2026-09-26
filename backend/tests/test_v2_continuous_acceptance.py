"""Paid semantic journeys through persisted sandbox delivery, never a real channel."""
import hashlib
import json
import os
from pathlib import Path
import httpx
import pytest
from sqlalchemy import select
from app.automation_models import AutomationSession, AutomationRun, RehearsalJob, RehearsalEnrollment
from app.automation_service import add_customer_message, queue_passive, process_automation_run, confirm_draft, sop_snapshot, advance_sops, dt, iso
from datetime import timedelta
from app.config import settings
from app.decision_service import generate_decision
from app.models import InboxBinding, KnowledgeVersion, MaterialAsset, StoredMedia, OutboundMessage, SopDefinition, utcnow
from app.material_library import CATALOG_VERSION
from app.reception_v2 import ENGINE_RELEASE_ID
from app.route_packages import ROUTES
from app.release_provenance import source_fingerprint, assert_source_unchanged

JOURNEYS = [
 ('opening9', ['你好，想了解一下你們的旅行', '選桃花9日', '在哪集合？']),
 ('opening11', ['您好', '我想看珠峰11日', '給我行程圖']),
 ('new_9', ['想了解桃花9日', '我們6位，3月28日出發', '價格多少？', '在哪集合？']),
 ('new_11', ['想去珠峰11日', '我們2位', '珠峰住哪？', '有獨立衛浴嗎？']),
 ('switch_up', ['給我9日行程圖', '我們4位', '改成11日去珠峰，給我新行程', '小費怎麼算？']),
 ('switch_down', ['給我11日行程圖', '不要珠峰了，改9日', '住宿也有供氧嗎？', '車是什麼車？']),
 ('pause_resume', ['想了解9日', '我先跟家人討論', '還有一個問題，在哪集合？', '我們2位']),
 ('no_line', ['想了解9日', '不想留LINE，在哪集合？', '入藏函在哪拿？', '微信是付款嗎？']),
 ('party_correction', ['我們2位想去9日', '改成6位一起', '那6人價格多少？', '房間幾人一間？']),
 ('date_correction', ['想去9日，3月28日', '改9月去可以嗎？', '那還是3月底好了', '需要在成都集合嗎？']),
 ('age_boundary', ['想去11日', '媽媽台灣人75歲', '需要什麼證明？', '爸爸76歲也能嗎？']),
 ('health', ['想去11日', '媽媽台灣人65歲', '供氧就不會高反嗎？', '高反藥吃多少？']),
 ('rail', ['想去9日', '可以坐青鐵入藏嗎？', '那回程坐呢？', '票包含嗎？']),
 ('permit_transfer', ['想去11日', '入藏函在哪拿？', '成都交函就是成都集合嗎？', '不經成都從重慶飛怎麼辦？']),
 ('hotel_exception', ['想去11日', '全程希爾頓嗎？', '波密也是嗎？', '珠峰那晚呢？']),
 ('price_scope', ['我們6位想去9日', '9980是起價嗎？', '機票包含嗎？', '小費司導各30嗎？']),
 ('culture', ['想去9日', '除了桃花還有什麼？', '扎基寺會去嗎？', '拜了就會發財嗎？']),
 ('intro9', ['我們4位想去9日', '完整介紹，行程圖酒店車的資料都要', '集合在哪？', '日期還沒確定']),
 ('intro11', ['我們2位想去11日', '完整介紹，行程圖酒店車的資料都要', '珠峰房間有氧嗎？', '我先跟家人討論']),
 ('attachment', ['想去9日', '高反能預防嗎？', '我要高反說明PDF附件']),
 ('contact_time', ['想去11日', '我先考慮一下', '後天早上10點再聯繫我']),
 ('global_stop', ['想去9日', '先看看住宿', '不要再主動聯繫我', '但我還想問在哪集合？']),
 ('wechat_capture', ['我們2位想去9日', '在哪集合？', '微信是test_traveller26，請顧問聯絡我']),
 ('email_capture', ['我們4位想去11日', '珠峰住哪？', '我的Email是traveller@example.com，請顧問聯絡我']),
]


CRITICAL_JOURNEYS={'new_9','party_correction','date_correction','switch_up','switch_down','pause_resume','no_line','age_boundary','health',
    'rail','permit_transfer','intro9','intro11','attachment','contact_time','global_stop','wechat_capture','email_capture'}
FINAL_BATCH=os.environ.get('V2_JOURNEY_FINAL')=='1'
SHARDS=int(os.environ.get('V2_JOURNEY_SHARDS','1'))
SHARD=int(os.environ.get('V2_JOURNEY_SHARD','0'))
JOURNEY_RUNS=[(name,turns,repeat+1) for i,(name,turns) in enumerate(JOURNEYS) if i%SHARDS==SHARD
    for repeat in range((5 if name in CRITICAL_JOURNEYS else 3) if FINAL_BATCH else 1)]


@pytest.mark.skipif(os.environ.get('VERIFY_V2_JOURNEYS') != '1', reason='Explicit model-only paid acceptance')
@pytest.mark.parametrize('name,turns,repeat', JOURNEY_RUNS, ids=[f'{x[0]}-r{x[2]}' for x in JOURNEY_RUNS])
def test_real_v2_persisted_customer_journey(session_factory, monkeypatch, tmp_path, name, turns, repeat):
    from app.reply_fact_verification import _VERIFIER_CACHE
    _VERIFIER_CACHE.clear()  # Each repeat must independently exercise the auditors.
    original, async_original = httpx.Client.send, httpx.AsyncClient.send
    allowed = settings.deepseek_base_url.rstrip('/') + '/chat/completions'
    def send(client, request, *a, **kw):
        assert str(request.url) == allowed and request.method == 'POST'
        return original(client, request, *a, **kw)
    async def async_send(client, request, *a, **kw):
        assert str(request.url) == allowed and request.method == 'POST'
        return await async_original(client, request, *a, **kw)
    monkeypatch.setattr(httpx.Client, 'send', send)
    monkeypatch.setattr(httpx.AsyncClient, 'send', async_send)
    report = {'id': name, 'repeat':repeat, 'release': ENGINE_RELEASE_ID, 'turns': [], 'real_customer_messages': 0,
              'source_fingerprint':source_fingerprint(),'fixture_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    def model(context):
        try:
            d, calls, digest, trace = generate_decision(context)
        except Exception as exc:
            report['model_error']={'input':context['customer_text'],'type':type(exc).__name__,
                                   'message':str(exc)[:1500]}
            raise
        report['turns'].append({'input': context['customer_text'], 'decision': __import__('dataclasses').asdict(d), 'trace': trace})
        return d, calls, digest, trace
    monkeypatch.setattr('app.automation_service.generate_decision', model)
    try:
        with session_factory() as db:
            inbox = InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name='isolated', channel_type='facebook')
            version = KnowledgeVersion(tenant_id=1, version_key=CATALOG_VERSION, title='isolated fixture', content_hash='fixture')
            db.add_all([inbox, version]); db.flush()
            keys = {key for route in ROUTES.values() for g in route['groups'].values() for key in g['assets'] if key != 'china2go-altitude-guide-v1'}
            from PIL import Image
            for index, key in enumerate(sorted(keys)):
                path = tmp_path / f'{index}.png'
                Image.new('RGB',(16,16),(index*7 % 255, index*13 % 255,80)).save(path)
                media = StoredMedia(tenant_id=1, created_by=1, original_name=path.name, media_type='image',
                    mime_type='image/png', file_size=path.stat().st_size, storage_path=str(path))
                db.add(media); db.flush()
                routes = [r for r,spec in ROUTES.items() if any(key in g['assets'] for g in spec['groups'].values())]
                narrative = '\n'.join(dict.fromkeys(g['text'] for spec in ROUTES.values() for g in spec['groups'].values() if key in g['assets']))
                db.add(MaterialAsset(knowledge_version_id=version.id, asset_key=key, source_path=str(path),
                    display_name=key, available=True, media_type='image', file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                    metadata_json={'stored_media_id':media.id, 'review_state':'evaluation_ready', 'live_approved':True,
                        'route_variants':routes, 'content_family':key, 'what_it_shows':narrative}))
            for route,spec in ROUTES.items():
                sop = SopDefinition(tenant_id=1, created_by=1, name=spec['sop']['name'], status='running',
                    route_variant=route, inbox_ids=[128859], nodes=spec['sop']['nodes'])
                db.add(sop); db.flush(); sop_snapshot(db,sop,1)
            session = AutomationSession(owner_id=1, inbox_binding_id=1, mode='journey', environment='playground',
                engine_version='v2', engine_release_id=ENGINE_RELEASE_ID, virtual_now='2026-09-20T02:00:00+00:00',
                controls={'can_reply':True,'ai_enabled':True,'channel':'facebook','labels':[], 'human':False,'history_complete':True})
            db.add(session); db.commit()
            for index, customer in enumerate(turns):
                add_customer_message(db,session,customer,f'{name}:{index}')
                session.due_at=utcnow(); db.commit()
                assert queue_passive(db,environment='playground',session_id=session.id)
                assert process_automation_run(db,environment='playground',session_id=session.id)
                db.refresh(session)
                run=db.scalar(select(AutomationRun).where(AutomationRun.session_id==session.id).order_by(AutomationRun.id.desc()))
                report['last_run']={'status':run.status,'error':run.error_code,'trace':run.trace,'decision':run.decision}
                assert run.status=='completed', (name,index,run.error_code,run.trace)
                drafts=[m for m in session.messages if m.get('status')=='draft' and m.get('run_id')==run.id]
                if drafts:
                    session.virtual_now=max(session.virtual_now, max(m['created_at'] for m in drafts))
                    confirm_draft(db,session,str(drafts[0]['id']))
                db.commit(); db.expire_all(); db.refresh(session)
                assert not db.scalar(select(OutboundMessage.id))
                assert run.decision.get('reply'), (name,index,'no answer')
                assert all(e.get('source_message_id')==run.input_snapshot['source_message_ids'][-1]
                           for e in run.decision.get('v2_events',[]))
                report['turns'][-1]['persisted_journey']=session.controls.get('journey')
                report['turns'][-1]['delivery_statuses']=[m['status'] for m in session.messages if m.get('run_id')==run.id]
                if name=='new_9' and index==1:
                    saved=(session.controls.get('journey') or {}).get('slots',{})
                    assert '6' in str(saved.get('party_size'))
                    assert '28' in str(saved.get('departure_window')), ('departure not persisted',run.decision)
                if run.decision['action']=='handoff':
                    assert session.controls.get('handoff_tasks')
                    assert index==len(turns)-1, (name,index,'unexpected early handoff',run.decision)
            state=(session.controls.get('journey') or {}).get('slots',{}).get('_v2_state',{})
            if name=='global_stop': assert state.get('proactive_opt_out')
            if name=='pause_resume': assert 'reevaluate_at' not in state
            if name=='party_correction': assert '6' in str((session.controls['journey']['slots']).get('party_size'))
            # Advance the actual persisted scheduler from the final delivery. Do
            # not fabricate answer receipts or invoke the model out of band.
            if not session.controls.get('human'):
                session.virtual_now=iso(dt(session.virtual_now)+timedelta(seconds=61))
                session.due_at=None
                before=len([m for m in session.messages if m.get('direction')=='outgoing'])
                advance_sops(db,session); db.commit()
                while process_automation_run(db,environment='playground',session_id=session.id):
                    db.refresh(session)
                    proactive_run=db.scalar(select(AutomationRun).where(AutomationRun.session_id==session.id).order_by(AutomationRun.id.desc()))
                    report['proactive_run']={'status':proactive_run.status,'error':proactive_run.error_code,'trace':proactive_run.trace,'decision':proactive_run.decision}
                    assert proactive_run.status == 'completed', report['proactive_run']
                drafts=[m for m in session.messages if m.get('status')=='draft']
                if drafts:
                    session.virtual_now=max(session.virtual_now,max(m['created_at'] for m in drafts))
                    confirm_draft(db,session,str(drafts[0]['id']))
                db.commit(); db.refresh(session)
                if state.get('proactive_opt_out') or state.get('reevaluate_at') or state.get('contact_at'):
                    assert len([m for m in session.messages if m.get('direction')=='outgoing'])==before
                report['scheduled_jobs']=[{'status':j.status,'reason':j.reason,'at':j.scheduled_at}
                    for j in db.scalars(select(RehearsalJob).join(RehearsalEnrollment).where(RehearsalEnrollment.session_id==session.id))]
            assert_source_unchanged(report['source_fingerprint'])
            report['source_unchanged']=True
            report['pass']=True
    except Exception as exc:
        report['pass']=False
        report['error']=f'{type(exc).__name__}: {str(exc)[:1500]}'
        raise
    finally:
        destination=Path(os.environ.get('V2_JOURNEY_OUTPUT','../output/v2-completion-20260920/journeys-pilot'))
        if FINAL_BATCH:
            destination=destination/f'r{repeat}'
        destination.mkdir(parents=True,exist_ok=True)
        (destination/f'{name}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
