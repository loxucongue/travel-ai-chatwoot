"""Prepare and run the supplied Xiamen FAQ through the real V3 playground.

An isolated database and runtime directory are mandatory. The optional model
env file contributes only model settings, never another company's data/config.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / 'businesses/rujing2/campaigns/xiamen-20261029'
ROUTE = 'xiamen_3_6d'


def read(name):
    return json.loads((PACK / name).read_text(encoding='utf8'))


def configure(args):
    from dotenv import dotenv_values
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.model_env:
        values = dotenv_values(Path(args.model_env).resolve())
        for key in ('DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_MODEL'):
            if values.get(key):
                os.environ[key] = values[key]
    os.environ.update(BUSINESS_PROFILE='rujing2', APP_PROFILE='evaluation', APP_ENV='development',
        OUTBOUND_MODE='disabled', CHATWOOT_WRITE_ENABLED='false', LIVE_SOP_ENABLED='false',
        LIVE_REPLY_ACCOUNT_ID='0', LIVE_REPLY_INBOX_ID='0', RELAY_BASE_URL='', RELAY_API_TOKEN='',
        DATABASE_URL='sqlite:///' + (output / 'playground.db').as_posix(),
        RUNTIME_PACKAGE_DIR=str(output / 'route-packages'), UPLOAD_DIR=str(output / 'uploads'),
        BUSINESS_MATERIALS_DIR=str(PACK / 'assets'), SESSION_COOKIE_SECURE='false',
        APP_SECRET_KEY=hashlib.sha256(str(output).encode()).hexdigest(),
        DEEPSEEK_TIMEOUT_SECONDS='45', MODEL_TEST_COST_LEDGER=str(output / 'cost.json'),
        PUBLIC_API_BASE_URL=f'http://127.0.0.1:{args.port}',
        FRONTEND_ORIGINS=f'http://127.0.0.1:{args.port}',
        LOG_LEVEL='WARNING')
    ledger = output / 'cost.json'
    if not ledger.exists():
        ledger.write_text(json.dumps({'limit_cny': args.budget, 'calls': []}), encoding='utf8')
    sys.path.insert(0, str(ROOT / 'backend'))
    os.chdir(ROOT / 'backend')
    return output


def prepare(output):
    if (output / 'playground.db').exists():
        raise ValueError('existing_rehearsal_database_use_run_or_serve')
    from app.main import app  # register all tables
    from app.db import Base, engine, SessionLocal
    from app.models import Tenant, User, InboxBinding, KnowledgeVersion, MaterialAsset, StoredMedia
    from app.security import hash_password
    from app.route_packages import install_route_package, _validate
    from app.reception_config import default_reception_configuration, ReceptionConfiguration, put_reception_configuration
    from app.operations import save_setting
    from app.reception_v3.skills import compile_skills
    package = read('route-package.json')
    _validate(deepcopy(package), PACK / 'route-package.json')
    Base.metadata.create_all(engine)
    install_route_package(package)
    password = secrets.token_urlsafe(16)
    with SessionLocal() as db:
        db.add(Tenant(id=1, name='廈門行程演練', timezone='Asia/Taipei', ai_enabled=False))
        db.add(User(id=1, email='rehearsal@rujing2.example.com', display_name='業務演練', role='super_admin',
                    password_hash=hash_password(password)))
        db.flush()
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=1, name='本地演練收件箱',
                            channel_type='sandbox', ai_enabled=False))
        version = KnowledgeVersion(tenant_id=1, version_key=package['knowledge_version'], title=package['name'],
                                   content_hash=hashlib.sha256(json.dumps(package, sort_keys=True).encode()).hexdigest(),
                                   source_summary={'source': 'user-doc-20261029', 'scope': 'rehearsal-only'})
        db.add(version)
        db.flush()
        for item in read('materials.json')['items']:
            path = PACK / 'assets' / item['filename']
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item['sha256']:
                raise ValueError('source_material_hash_mismatch')
            media = StoredMedia(tenant_id=1, original_name=item['filename'], media_type=item['media_type'],
                mime_type=mimetypes.guess_type(path)[0] or 'application/octet-stream',
                file_size=path.stat().st_size, storage_path=str(path), created_by=1)
            db.add(media)
            db.flush()
            db.add(MaterialAsset(knowledge_version_id=version.id, asset_key=item['key'], source_path=str(path),
                display_name=item['display_name'], media_type=item['media_type'], usage=item['usage'],
                file_hash=digest, available=True, metadata_json={**item, 'stored_media_id': media.id,
                    'route_variants': [ROUTE], 'content_family': item['key'], 'live_approved': False}))
        config = default_reception_configuration()
        for section, values in read('reception-config.json').items():
            if isinstance(values, dict):
                config[section].update(values)
            else:
                config[section] = deepcopy(values)
        put_reception_configuration(db, ReceptionConfiguration.model_validate(config))
        save_setting(db, 'global_message_sending', {'enabled': False})
        db.commit()
        bundle = compile_skills(db)
        assert list(bundle['routes']) == [ROUTE] and bundle['currency'] == 'TWD'
        (output / 'compiled-skills.json').write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf8')
    (output / 'login.json').write_text(json.dumps({'email': 'rehearsal@rujing2.example.com', 'password': password}, indent=2), encoding='utf8')
    print(json.dumps({'prepared': True, 'faq': 35, 'materials': 8, 'outbound': False}), flush=True)


def settle(db, row):
    from sqlalchemy import select
    from app.automation_models import AutomationRun
    from app.reception_v3 import service
    for _ in range(35):
        # Advance only this sandbox clock to drain delivery gaps; no real sleep.
        wall = service.later(row.controls['simulation']['last_wall_at'], 3)
        service.tick(db, session_id=row.id, wall_now=wall)
        db.refresh(row)
        runs = db.scalars(select(AutomationRun).where(AutomationRun.session_id == row.id)).all()
        failure = next((run.error_code for run in runs if run.status == 'failed'), None)
        if failure:
            raise RuntimeError(failure)
        state = service.state(row)
        if state.get('handoff') or (not state.get('pending_event') and not any(m.get('status') == 'draft' for m in row.messages)):
            return runs
    raise RuntimeError('rehearsal_did_not_settle')


def run(output, ids):
    from sqlalchemy import select
    from app.config import settings
    if not settings.deepseek_api_key:
        raise RuntimeError('model_key_not_configured')
    from app.main import app
    from app.db import SessionLocal
    from app.models import utcnow
    from app.automation_models import AutomationSession, AutomationRun
    from app.reception_v3 import service
    faqs = read('faq.json')['items']
    selected = set(ids.split(',')) if ids else {f['id'] for f in faqs}
    cases = [{'id': f['id'], 'title': f['name'], 'messages': [f['question']],
              'expected': 'handoff' if f['handling'] == 'handoff' else 'reply'} for f in faqs if f['id'] in selected]
    extra = [
        {'id': 'hotel_material', 'title': '酒店圖文', 'messages': ['廈門住什麼飯店？給我看房間照片。'], 'expected': 'reply', 'asset': 'xiamen_hotel'},
        {'id': 'food_video', 'title': '餐食影片', 'messages': ['廈門餐食有影片可以看嗎？'], 'expected': 'reply', 'asset': 'xiamen_meal_video'},
        {'id': 'eight_people', 'title': '8人不套舊公司交接門檻', 'messages': ['我們8位想去廈門，你們一般一團幾人？'], 'expected': 'reply'},
        {'id': 'fb_continuity', 'title': '拒絕加好友後仍可問價', 'messages': ['我沒有LINE也沒有微信，只在FB聊可以嗎？', '那廈門一個人多少錢？有含機票嗎？'], 'expected': 'reply'},
        {'id': 'date_price', 'title': '提供月份不虛構精確價', 'messages': ['廈門5天一個人多少錢？', '我們兩個人，明年3月。'], 'expected': 'reply'},
        {'id': 'introduction', 'title': '概覽不編造逐日安排', 'messages': ['我想選廈門行程，先給我看你們現有的介紹。'], 'expected': 'reply'},
        {'id': 'conversion_price', 'title': '答價後自然邀請私訊', 'messages': ['廈門一個人多少錢？'], 'expected': 'reply', 'invitation': True},
        {'id': 'conversion_agency', 'title': '資質需求引導私訊', 'messages': ['你們是哪家公司？有營業執照嗎？'], 'expected': 'reply', 'invitation': True},
        {'id': 'line_contact', 'title': 'LINE ID立即交接', 'messages': ['廈門一個人多少錢？', '我的LINE ID是 test_travel_2026'], 'expected': 'handoff', 'handoff_type': 'contact'},
        {'id': 'wechat_contact', 'title': '微信ID立即交接', 'messages': ['我的微信號是 wx_test_2026，想了解廈門'], 'expected': 'handoff', 'handoff_type': 'contact'},
        {'id': 'consent_only', 'title': '同意加好友不等於已留資', 'messages': ['想了解廈門，方便加LINE嗎？', '好，可以加'], 'expected': 'reply'},
        {'id': 'qr_contact', 'title': '明確聯絡QR圖片交人工核驗', 'messages': ['想了解廈門，方便加LINE嗎？',
            {'text':'這是我的LINE QR Code，請加我。', 'attachments':[{'id':90001,'file_type':'image','file_name':'contact.png'}]}],
            'expected':'handoff','handoff_type':'contact_image'},
        {'id': 'ordinary_photo', 'title': '普通照片不作留資成功', 'messages': ['想了解廈門酒店',
            {'text':'這種房間可以嗎？', 'attachments':[{'id':90002,'file_type':'image','file_name':'room.png'}]}], 'expected':'reply'},
        {'id': 'requested_human', 'title': '主動要求真人不強制先留資', 'messages':['我要直接找真人客服'], 'expected':'handoff','handoff_type':'requested'},
        {'id': 'contact_no_repeat', 'title': '連續答疑不每輪催ID', 'messages':['廈門一個人多少錢？','機票有包含嗎？','那你們一般幾人一團？'], 'expected':'reply','max_invitations':1},
        {'id': 'sales_handoff_journey', 'title': '客人補需求仍以專人私訊為主',
            'messages':['想了解雲南行程，請提供營業執照、合同退費規定和公司收款資料。','抱歉，是廈門！','預計4/2–4/7','不要全都是風景','有9歲、12歲的孩童隨行'],
            'expected':'reply','max_invitations':2},
        {'id': 'seven_day_followup', 'title': '每日一次最多7天且圖文影片去重', 'messages':['我先看看廈門的美食和風景，有資料可以分享'], 'expected':'reply','followup_days':7},
        {'id': 'conversion_optout', 'title': '拒絕聯絡停止追訪', 'messages':['想了解廈門','不要再聯絡我，也不要發宣傳'], 'expected':'reply','opt_out':True},
    ]
    cases.extend(case for case in extra if not ids or case['id'] in selected)
    for case in cases:
        started = time.monotonic()
        with SessionLocal() as db:
            row = AutomationSession(owner_id=1, inbox_binding_id=1, environment='playground', mode='journey',
                engine_version='v3', virtual_now=utcnow(), controls={'route_variant': ROUTE}, messages=[], memory={})
            db.add(row)
            db.flush()
            service.start(db, row, case['messages'][0])
            sid = row.id
            db.commit()
            error = None
            try:
                runs = settle(db, row)
                for index, message in enumerate(case['messages'][1:], 1):
                    content = message if isinstance(message, str) else message['text']
                    attachments = None if isinstance(message, str) else message.get('attachments')
                    service.add_message(db, row, content, f'case-{case["id"]}-{index}',
                                        content_type='image' if attachments else 'text', attachments=attachments)
                    db.commit()
                    runs = settle(db, row)
                for _ in range(case.get('followup_days', 0)):
                    if not service.state(row).get('next_check_at'):
                        break
                    service.advance_next(row)
                    db.commit()
                    runs = settle(db, row)
            except Exception as exc:
                error = str(exc)
            row = db.get(AutomationSession, sid)
            state = service.state(row)
            messages = row.messages
            delivered = [m for m in messages if m.get('status') == 'simulated_delivered']
            reply = [m for m in delivered if m.get('v3_kind') != 'opening']
            actual = 'handoff' if state.get('handoff') else 'reply' if reply else 'wait'
            issues = []
            if error:
                issues.append(error)
            if actual != case['expected']:
                issues.append(f'expected_{case["expected"]}_got_{actual}')
            if case.get('handoff_type') and state.get('handoff_type') != case['handoff_type']:
                issues.append('wrong_handoff_type')
            invitations = [m for m in reply if m.get('contact_invitation')]
            if case.get('invitation') and not invitations:
                issues.append('contact_invitation_missing')
            if len(invitations) > case.get('max_invitations', 100):
                issues.append('repeated_contact_invitation')
            if case.get('opt_out') and (not state.get('opt_out') or state.get('next_check_at')):
                issues.append('optout_not_stopped')
            if case.get('followup_days'):
                from zoneinfo import ZoneInfo
                touches = [m for m in reply if m.get('proactive')]
                by_date = {}
                for m in touches:
                    day = service.date(m['created_at']).astimezone(ZoneInfo('Asia/Taipei')).date().isoformat()
                    by_date.setdefault(day, set()).add(m['run_id'])
                if any(len(ids) > 1 for ids in by_date.values()) or len(by_date) > 7:
                    issues.append('daily_or_seven_day_limit_exceeded')
                if not touches:
                    issues.append('no_followup_delivered')
                keys = [m['asset_key'] for m in reply if m.get('asset_key')]
                if len(keys) != len(set(keys)):
                    issues.append('repeated_material')
                if state.get('next_check_at'):
                    issues.append('followup_not_stopped_after_seven_days')
            if case.get('asset') and not any(m.get('asset_key') == case['asset'] for m in reply):
                issues.append('expected_asset_missing')
            text = '\n'.join(m.get('content', '') for m in reply)
            if any(word in text for word in ('China2Go', '西藏', '入藏', '人民幣', '人民币', '申請款', '申请款')):
                issues.append('unrelated_or_unapproved_claim')
            runs = db.scalars(select(AutomationRun).where(AutomationRun.session_id == sid)).all()
            result = {**case, 'session_id': sid, 'actual': actual, 'issues': issues,
                      'package_sha256': hashlib.sha256((PACK / 'route-package.json').read_bytes()).hexdigest(),
                      'duration_seconds': round(time.monotonic()-started, 2), 'messages': messages,
                      'handoff_reason': state.get('handoff_reason'),
                      'runs': [{'id': r.id, 'status': r.status, 'error': r.error_code, 'decision': r.decision,
                                'trace': r.trace} for r in runs]}
            with (output / 'results.jsonl').open('a', encoding='utf8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
            print(json.dumps({k: result[k] for k in ('id', 'session_id', 'actual', 'issues', 'duration_seconds')}, ensure_ascii=False), flush=True)
            if error:
                # Stop the failed sandbox case so a later viewer worker cannot retry it.
                service.control(row, 'stop')
                db.commit()
                break


def refresh(output):
    from app.main import app
    from app.db import SessionLocal
    from app.models import User
    from app.route_packages import install_route_package
    from app.reception_config import get_reception_configuration, ReceptionConfiguration, put_reception_configuration
    install_route_package(read('route-package.json'))
    with SessionLocal() as db:
        user = db.get(User, 1)
        if user and user.email == 'rehearsal@rujing2.local':
            user.email = 'rehearsal@rujing2.example.com'
            login_path = output / 'login.json'
            login = json.loads(login_path.read_text(encoding='utf8'))
            login['email'] = user.email
            login_path.write_text(json.dumps(login, indent=2), encoding='utf8')
        config = get_reception_configuration(db)
        for section, values in read('reception-config.json').items():
            if isinstance(values, dict):
                config[section].update(values)
            else:
                config[section] = deepcopy(values)
        put_reception_configuration(db, ReceptionConfiguration.model_validate(config))
        db.commit()
    print('rehearsal_company_configuration_refreshed', flush=True)


def serve(output, port):
    from app.main import app
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    dist = ROOT / 'frontend/dist'
    app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')
    @app.get('/{path:path}')
    def index(path: str):
        from fastapi import HTTPException
        if path.startswith('v1/'):
            raise HTTPException(status_code=404)
        return FileResponse(dist / 'index.html')
    log = (output / 'worker.log').open('a', encoding='utf8')
    child = subprocess.Popen([sys.executable, '-m', 'app.playground_worker'], stdout=log, stderr=log,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    (output / 'service-pids.json').write_text(json.dumps({'server': os.getpid(), 'worker': child.pid}), encoding='utf8')
    try:
        uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')
    finally:
        child.terminate()
        child.wait(timeout=15)
        log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'refresh', 'run', 'serve'])
    parser.add_argument('--output', required=True)
    parser.add_argument('--model-env')
    parser.add_argument('--cases', default='')
    parser.add_argument('--budget', type=float, default=5)
    parser.add_argument('--port', type=int, default=8013)
    args = parser.parse_args()
    output = configure(args)
    if args.action == 'prepare':
        prepare(output)
    elif args.action == 'refresh':
        refresh(output)
    elif args.action == 'run':
        run(output, args.cases)
    else:
        serve(output, args.port)


if __name__ == '__main__':
    main()
