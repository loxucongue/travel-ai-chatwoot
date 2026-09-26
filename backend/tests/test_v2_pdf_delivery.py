"""Approved PDF binding and actual worker delivery, using an isolated channel."""
import hashlib
from pathlib import Path
import pytest
from sqlalchemy import select
from app.models import StoredMedia, MaterialAsset, ConversationState, HandoffTask, OutboundMessage
from app.live_reply_models import LiveReplyJob
from app.reception_v2 import ENGINE_RELEASE_ID
from app.deepseek_evaluation import EvaluationDecision
from app.reception_v2.runtime import _enforce_delivery_contract

ROUTE = 'peach_9d_2027'
KEY = 'china2go-altitude-guide-v1'


def pdf_asset(db, tmp_path, approved=False):
    from app.automation_api import _route_asset
    from app.material_library import replace_asset_binding
    path = tmp_path / 'guide.pdf'
    path.write_bytes(b'%PDF-1.4\n% isolated attachment fixture\n%%EOF')
    media = StoredMedia(tenant_id=1, created_by=1, original_name=path.name, media_type='file',
        mime_type='application/pdf', file_size=path.stat().st_size, storage_path=str(path))
    db.add(media); db.flush()
    _, asset = _route_asset(db, ROUTE, KEY, create_missing=True)
    replace_asset_binding(db, asset, media)
    if approved:
        asset.metadata_json = {**asset.metadata_json, 'live_approved':True, 'review_state':'evaluation_ready'}
    db.commit()
    return asset


def test_pdf_approval_requires_reviewed_current_bytes(authenticated, session_factory, tmp_path):
    client, csrf = authenticated
    with session_factory() as db:
        asset = pdf_asset(db, tmp_path)
        digest = asset.file_hash
    url = f'/v1/automation/route-products/{ROUTE}/assets/{KEY}/approve'
    headers = {'X-CSRF-Token':csrf}
    assert client.post(url, headers=headers, json={'expected_hash':digest, 'review_notes':'short'}).status_code == 422
    assert client.post(url, headers=headers, json={'expected_hash':'0'*64, 'review_notes':'Reviewed the complete approved attachment.'}).status_code == 409
    response = client.post(url, headers=headers, json={'expected_hash':digest, 'review_notes':'Reviewed the complete approved attachment.'})
    assert response.status_code == 200, response.text
    with session_factory() as db:
        asset = db.scalar(select(MaterialAsset).where(MaterialAsset.asset_key == KEY))
        assert asset.metadata_json['approved_file_hash'] == digest
        assert asset.metadata_json['live_approved'] is True
        from app.material_library import resolve_materials
        assert resolve_materials(db,[KEY],'peach_11d_2027',1)[0]['content_type'] == 'file'
        Path(asset.source_path).write_bytes(b'changed after preview')
    assert client.post(url, headers=headers, json={'expected_hash':digest, 'review_notes':'Reviewed the complete approved attachment.'}).status_code == 409


@pytest.mark.parametrize('mime,expected,disposition',[
    ('application/pdf','application/pdf','inline'),
    ('text/html','application/octet-stream','attachment'),
    ('image/svg+xml','application/octet-stream','attachment'),
])
def test_file_preview_has_correct_type_filename_and_security_headers(authenticated,session_factory,tmp_path,mime,expected,disposition):
    client,_=authenticated
    path=tmp_path/'review.pdf'
    path.write_bytes(b'%PDF-1.4\n%%EOF')
    with session_factory() as db:
        media=StoredMedia(tenant_id=1,created_by=1,original_name='review.pdf',media_type='file',
            mime_type=mime,file_size=path.stat().st_size,storage_path=str(path))
        db.add(media);db.commit();media_id=media.id
    response=client.get(f'/v1/media/{media_id}/preview')
    assert response.status_code==200
    assert response.headers['content-type']==expected
    assert response.headers['content-disposition']==f'{disposition}; filename="review.pdf"'
    assert response.headers['content-security-policy']=='sandbox'
    assert response.headers['x-content-type-options']=='nosniff'
    assert response.headers['cache-control']=='private, no-store'


@pytest.mark.parametrize('fault', ['none', 'revoked', 'unknown'])
def test_contact_capture_sends_actual_pdf_once_or_stops(session_factory, monkeypatch, tmp_path, fault):
    import httpx
    import app.live_reply as live
    from test_live_reply import setup
    fake = setup(session_factory, monkeypatch)
    fake.messages[0]['content'] = '我的微信是test_travel，請顧問聯絡'
    with session_factory() as db:
        asset = pdf_asset(db, tmp_path, approved=True)
        state, job = db.get(ConversationState,1), db.get(LiveReplyJob,1)
        state.ai_engine_version = job.engine_version = 'v2'
        state.ai_engine_release_id = job.engine_release_id = ENGINE_RELEASE_ID
        db.commit()
    decision = EvaluationDecision('handoff','peach_9d','contact',route_variant=ROUTE,
        reply='收到',lead_action='captured',contact_values={'wechat':'test_travel'},handoff_reason='lead_captured')
    _enforce_delivery_contract({'available_materials':[{'key':KEY}]},decision)
    monkeypatch.setattr(live,'generate_decision',lambda _: (decision,[],'fixture',{}))
    sent_files = []
    def send_file(conversation, content, path, mime):
        assert mime == 'application/pdf' and Path(path).read_bytes().startswith(b'%PDF')
        sent_files.append(path)
        if fault == 'unknown': raise httpx.ReadTimeout('unknown submission')
        return {'id':2000,'attachments':[{'file_type':'file','data_url':'https://example.invalid/guide.pdf'}]}
    monkeypatch.setattr(fake,'create_attachment_message',send_file)
    original = fake.create_text_message
    def send_text(*args):
        result = original(*args)
        if fault == 'revoked':
            with session_factory() as db:
                asset = db.scalar(select(MaterialAsset).where(MaterialAsset.asset_key == KEY))
                asset.metadata_json = {**asset.metadata_json,'live_approved':False}
                db.commit()
        return result
    monkeypatch.setattr(fake,'create_text_message',send_text)
    original_complete = live.complete
    def complete(*args, **kwargs):
        import sys, traceback
        if sys.exc_info()[0]: traceback.print_exc()
        return original_complete(*args, **kwargs)
    monkeypatch.setattr(live, 'complete', complete)
    live.process_job(1)
    live.process_job(1)
    assert len(sent_files) == (0 if fault == 'revoked' else 1)
    with session_factory() as db:
        job = db.get(LiveReplyJob,1)
        assert job.status == {'none':'handoff','revoked':'blocked','unknown':'submission_unknown'}[fault], (job.status,job.error_code)
        assert db.scalar(select(HandoffTask)) is not None
        if fault == 'none':
            assert db.scalar(select(OutboundMessage).where(OutboundMessage.content_type == 'file')) is not None
    assert 'ai' not in {label.lower() for label in fake.labels}
