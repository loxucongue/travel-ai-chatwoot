"""Exercise interruption at real persisted multipart worker boundaries."""
import hashlib
import httpx
import pytest
from sqlalchemy import select
import app.live_reply as live
from app.deepseek_evaluation import EvaluationDecision
from app.live_reply_models import LiveReplyJob
from app.models import ConversationState, HandoffTask, MaterialAsset, StoredMedia, OutboundMessage, utcnow
from app.reception_v2 import ENGINE_RELEASE_ID
from app.reception_v2.material_delivery import introduction_sections
from app.route_packages import ROUTES
from test_live_reply import setup, add_image, add_itinerary_progress


FAULTS = ['new_customer', 'ai_removed', 'human_task', 'cannot_reply', 'engine_switch',
          'release_change', 'file_missing', 'file_changed', 'approval_revoked', 'submission_unknown', 'delivery_failed']


@pytest.mark.parametrize('fault', FAULTS)
def test_full_intro_stops_and_does_not_retry_uncertain_parts(session_factory, monkeypatch, tmp_path, fault):
    fake=setup(session_factory,monkeypatch)
    route='peach_9d_2027'
    keys={key for group in ROUTES[route]['groups'].values() for key in group['assets'] if key != 'china2go-altitude-guide-v1'}
    with session_factory() as db:
        first=db.get(MaterialAsset,add_image(db,tmp_path))
        for key in sorted(keys-{'routes12-9d-itinerary'}):
            path=tmp_path/f'{key}.png'; path.write_bytes(f'fixture:{key}'.encode())
            media=StoredMedia(tenant_id=1,created_by=1,original_name=path.name,media_type='image',
                mime_type='image/png',file_size=path.stat().st_size,storage_path=str(path))
            db.add(media); db.flush()
            db.add(MaterialAsset(knowledge_version_id=first.knowledge_version_id,asset_key=key,
                source_path=str(path),display_name=key,available=True,file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                metadata_json={'stored_media_id':media.id,'review_state':'evaluation_ready',
                    'live_approved':True,'route_variants':[route],'content_family':key}))
        state=db.get(ConversationState,1); job=db.get(LiveReplyJob,1)
        state.ai_engine_version=job.engine_version='v2'
        state.ai_engine_release_id=job.engine_release_id=ENGINE_RELEASE_ID
        db.commit(); add_itinerary_progress(db)
    sections=introduction_sections(ROUTES[route],keys,{})
    decision=EvaluationDecision('reply','peach_9d','itinerary',reply=sections[0]['text'],route_variant=route,
        content_group_key='brand_positioning',covered_content_groups=[s['group_key'] for s in sections],
        material_keys=list(keys),v2_delivery_sections=sections)
    monkeypatch.setattr(live,'generate_decision',lambda _:(decision,[],'hash',{}))
    changed=False
    def disrupt(_):
        nonlocal changed
        if changed: return
        changed=True
        if fault=='new_customer': fake.messages.append({'id':101,'created_at':utcnow(),'message_type':0,'content':'等等'})
        elif fault=='ai_removed': fake.labels=[]
        elif fault=='cannot_reply': fake.can_reply=False
        elif fault in {'submission_unknown','delivery_failed'}:
            original=fake.create_attachment_message
            def send(*args):
                result=original(*args)
                if fault=='submission_unknown': raise httpx.ReadTimeout('simulated uncertain submission')
                return {**result,'status':'failed'}
            monkeypatch.setattr(fake,'create_attachment_message',send)
        else:
            with session_factory() as db:
                state=db.get(ConversationState,1)
                if fault=='human_task': db.add(HandoffTask(conversation_state_id=1,reason_code='manual',status='pending'))
                elif fault=='engine_switch': state.ai_engine_version='v1'
                elif fault=='release_change': state.ai_engine_release_id='new-release'
                else:
                    asset=db.scalar(select(MaterialAsset).where(MaterialAsset.asset_key=='routes12-9d-itinerary'))
                    from pathlib import Path
                    if fault=='file_missing': Path(asset.source_path).unlink()
                    elif fault=='file_changed': Path(asset.source_path).write_bytes(b'changed')
                    elif fault=='approval_revoked': asset.metadata_json={**asset.metadata_json,'live_approved':False}
                db.commit()
    monkeypatch.setattr(live.time,'sleep',disrupt)
    live.process_job(1)
    with session_factory() as db:
        job=db.get(LiveReplyJob,1)
        assert job.status in {'blocked','submission_unknown'}, (fault,job.status,job.error_code)
        assert len(fake.sent)==(2 if fault in {'submission_unknown','delivery_failed'} else 1)
        attempts=db.scalars(select(OutboundMessage)).all()
        assert all(len(o.content_attributes['_delivery_item']['group_keys'])==1 for o in attempts)
        count=len(fake.sent)
    live.process_job(1)
    assert len(fake.sent)==count, 'terminal/uncertain job must not resend any section'
