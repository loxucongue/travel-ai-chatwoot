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
    from app.business_text import read_business_json
    return read_business_json(PACK / name)


def verify_effective_configuration(db, output):
    """Test the installed settings, not just a newer file sitting next to them."""
    from app.business_text import validate_business_text, read_business_json
    from app.reception_config import get_reception_configuration, ReceptionConfiguration
    from app.reception_v3.skills import compile_skills
    from app.route_packages import ROUTE_PACKAGES, _validate
    current = get_reception_configuration(db)
    expected = deepcopy(current)
    for section, values in read('reception-config.json').items():
        if isinstance(values, dict):
            expected[section].update(values)
        else:
            expected[section] = deepcopy(values)
    expected = ReceptionConfiguration.model_validate(expected).model_dump()
    from app.opening_messages import opening_media_info
    from app.models import Tenant
    from sqlalchemy import select
    tenant = db.scalar(select(Tenant))
    for item in expected['reply'].get('opening_items') or []:
        if item['content_type'] != 'text':
            info = opening_media_info(db, item, tenant.id)
            item.update({k:info[k] for k in ('media_id', 'media_hash', 'media_name')})
    if expected != current:
        raise ValueError('rehearsal_configuration_stale_run_refresh')
    installed = read_business_json(ROUTE_PACKAGES[ROUTE]['source_path'])
    if installed != _validate(deepcopy(read('route-package.json')), PACK / 'route-package.json'):
        raise ValueError('rehearsal_route_stale_run_refresh')
    bundle = compile_skills(db)
    validate_business_text(bundle, 'effective_skills')
    (output / f"effective-skills-{bundle['digest']}.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf-8')
    return bundle['digest']


def configure(args):
    from dotenv import dotenv_values
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.model_env:
        values = dotenv_values(Path(args.model_env).resolve())
        for key in ('DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_MODEL',
                    'GEMINI_FALLBACK_ENABLED', 'GOOGLE_GEMINI_BASE_URL', 'GEMINI_API_KEY', 'GEMINI_MODEL',
                    'GEMINI_FALLBACK_PRIMARY_TIMEOUT_SECONDS', 'GEMINI_FALLBACK_COOLDOWN_SECONDS',
                    'QWEN_FALLBACK_ENABLED', 'QWEN_BASE_URL', 'QWEN_API_KEY', 'QWEN_MODEL',
                    'QWEN_VISION_MODEL', 'CUSTOMER_IMAGE_VISION_ENABLED'):
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
    with SessionLocal() as db:
        effective_digest = verify_effective_configuration(db, output)
    faqs = read('faq.json')['items']
    selected = set(ids.split(',')) if ids else {f['id'] for f in faqs}
    cases = [{'id': f['id'], 'title': f['name'], 'messages': [f['question']],
              'expected': 'handoff' if f['handling'] == 'handoff' else 'reply'} for f in faqs if f['id'] in selected]
    extra = [
        {'id':'single_route_entry','title':'只發1仍先收到完整主線圖文', 'messages':['1'],
            'expected':'reply','opening_count':5,'unexpected_text':['1是指','1是什麼','1」是','不太確定是指']},
        {'id':'email_contact','title':'Email立即交接', 'messages':['想了解廈門','請寄到 travel.test@example.com'],
            'expected':'handoff','handoff_type':'contact'},
        {'id':'line_link','title':'LINE連結立即交接不再要ID', 'messages':['想了解廈門','這是我的LINE加好友連結 https://line.me/ti/p/~test_travel_2026'],
            'expected':'handoff','handoff_type':'contact'},
        {'id':'no_apps_email','title':'無LINE微信可使用Email', 'messages':['想了解廈門','沒有LINE和微信，可以用Email嗎？'],
            'expected':'reply','invite_on_final_turn':True},
        {'id': 'hotel_material', 'title': '酒店圖文', 'messages': ['廈門住什麼飯店？給我看房間照片。'], 'expected': 'reply', 'asset': 'xiamen_hotel'},
        {'id': 'food_video', 'title': '餐食影片', 'messages': ['廈門餐食有影片可以看嗎？'], 'expected': 'reply', 'asset': 'xiamen_meal_video'},
        {'id': 'eight_people', 'title': '8人不套舊公司交接門檻或直接承諾獨團', 'messages': ['我們8位想去廈門，你們一般一團幾人？'], 'expected': 'reply',
            'unexpected_text': ['剛好可以自己成一團', '8位就可以獨立成團', '不會有其他人']},
        {'id': 'fb_continuity', 'title': '拒絕加好友後仍可問價', 'messages': ['我沒有LINE也沒有微信，只在FB聊可以嗎？', '那廈門一個人多少錢？有含機票嗎？'], 'expected': 'reply', 'no_invitation_after_turn': 0},
        {'id': 'date_price', 'title': '提供月份不虛構精確價', 'messages': ['廈門5天一個人多少錢？', '我們兩個人，明年3月。'], 'expected': 'reply'},
        {'id': 'introduction', 'title': '概覽不編造逐日安排且不重播整套介紹', 'messages': ['我想選廈門行程，先給我看你們現有的介紹。'], 'expected': 'reply', 'no_introduction_queue': True},
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
        {'id': 'contact_no_repeat', 'title': '連續答疑先答當前問題並自然推進', 'messages':['廈門一個人多少錢？','機票有包含嗎？','那你們一般幾人一團？'], 'expected':'reply'},
        {'id': 'sales_handoff_journey', 'title': '客人補需求仍以專人私訊為主',
            'messages':['想了解雲南行程，請提供營業執照、合同退費規定和公司收款資料。','抱歉，是廈門！','預計4/2–4/7','不要全都是風景','有9歲、12歲的孩童隨行'],
            'expected':'reply'},
        {'id': 'seven_day_followup', 'title': '每日一次最多7天且圖文影片去重', 'messages':['我先看看廈門的美食和風景，有資料可以分享'], 'expected':'reply','followup_days':7},
        {'id': 'conversion_optout', 'title': '拒絕聯絡停止追訪', 'messages':['想了解廈門','不要再聯絡我，也不要發宣傳'], 'expected':'reply','opt_out':True},
        {'id': 'sales_first_entry', 'title': '首輪了解產品即主動引導私訊',
            'messages': ['想了解廈門行程'], 'expected': 'reply', 'invitation': True},
        {'id': 'sales_first_considering', 'title': '未定日期和考慮中仍提供一對一私訊入口',
            'messages': ['我對廈門有興趣，不過時間還沒定，先考慮看看'],
            'expected': 'reply', 'invitation': True},
        {'id': 'sales_first_details', 'title': '要詳細行程不先要求收齊人數日期',
            'messages': ['能先給我詳細路線參考嗎？日期和人數都還沒確定。'],
            'expected': 'reply', 'invitation': True},
        {'id': 'sales_first_progression', 'title': '持續諮詢能再推進私訊而非只記下需求',
            'messages': ['廈門一個人多少錢？', '想明年四月去', '有9歲、12歲的小朋友同行',
                         '那適合孩子的詳細行程能給我參考嗎？'],
            'expected': 'reply', 'invitation': True, 'invite_on_final_turn': True,
            'unexpected_text': ['兩大兩小', '两大两小', '2大2小', '兩位大人', '2位大人']},
        {'id': 'sales_first_declined', 'title': '明確拒留資後繼續回答且不再催加',
            'messages': ['想了解廈門行程', '不用加LINE，也不要問我要其他聯絡方式，就在這裡回答。',
                         '那機票有包含嗎？'], 'expected': 'reply', 'no_invitation_after_turn': 1},
        {'id': 'sales_soft_variant', 'title': '不同措辭的未定意向仍可留資',
            'messages': ['有點心動，但還要和家人商量，我只是隨便問問'],
            'expected': 'reply', 'invitation': True},
        {'id': 'sales_busy_customer', 'title': '正在忙時尊重暫停而非再次索取',
            'messages': ['廈門一個人多少錢？', '我現在在開車，先不要發訊息，等我回來再問你。'],
            'expected': 'reply', 'no_invitation_after_turn': 1, 'followup_checks': 1,
            'no_proactive_after_turn': 1},
        {'id': 'sales_known_family', 'title': '人數已明確時承接但不自行算精確價',
            'messages': ['我們2位大人，孩子9歲和12歲，想明年四月去廈門，大概多少？'],
            'expected': 'reply', 'invitation': True},
        {'id': 'sales_unpriced_day', 'title': '一日遊不能套3至6天價格範圍',
            'messages': ['只想一天逛廈門，多少錢？'], 'expected': 'reply', 'invitation': True,
            'unexpected_text': ['5,000', '5000', '8,500', '8500']},
        {'id': 'sales_confirmed_departure', 'title': '指定日期未查庫存不得保證成行',
            'messages': ['明年4月5日一定有團嗎？如果我買機票了就保證能走嗎？'],
            'expected': 'reply', 'invitation': True},
        {'id': 'sales_private_group_question', 'title': '客人反問獨團時不把一般規模當承諾',
            'messages': ['我們8位想去廈門，你們一般一團幾人？', '那我們8位，是保證只有我們自己一團嗎？'],
            'expected': 'reply', 'invitation': True,
            'unexpected_text': ['保證只有你們', '保證只有您們', '8位就可以獨立成團']},
        {'id': 'sales_channel_change', 'title': '客人改變聯絡選擇可繼續留資',
            'messages': ['我先只在FB了解，不加其他聯絡。', '想了一下，請加我的LINE，ID是 test_resume_2026'],
            'expected': 'handoff', 'handoff_type': 'contact'},
        {'id': 'sales_followup_resume', 'title': '沉默觸達後客人回來繼續推進',
            'messages': ['想了解廈門美食，先給我看看'], 'followup_checks': 1,
            'resume_message': '我看了，還想了解住宿，時間沒定。', 'expected': 'reply',
            'invite_on_final_turn': True},
        {'id': 'contextual_progression', 'title': '不同新需求逐輪承接並邀請私訊',
            'messages':['想了解廈門詳細路線','預計明年4/2–4/7','不要全都是風景','有9歲、12歲的孩子同行'],
            'expected':'reply','invite_on_turns':[0,1,2,3],
            'unexpected_text':['兩大兩小','兩位大人','2位大人','加完馬上','一定不累']},
        {'id': 'contextual_ack', 'title': '收尾致謝不強行增加留資邀請',
            'messages':['廈門多少錢？','謝謝，先這樣'], 'expected':'reply','no_invitation_after_turn':1},
        {'id': 'contextual_preference', 'title': '已知LINE偏好接受條碼或連結',
            'messages':['想了解廈門詳細路線','我用LINE，沒有微信'],
            'expected':'reply','invite_on_final_turn':True},
        {'id': 'contextual_trust_refusal', 'title': '要求在原平台看資質不變相強迫留資',
            'messages':['我想看營業執照、收款戶名和退費規定','我不留私人聯絡，就在這裡給我看資料','那你們有購物站嗎？'],
            'expected':'reply','no_invitation_after_turn':1},
    ]
    if os.environ.get('REHEARSAL_LINE_QR_URL'):
        extra.append({'id':'actual_line_qr','title':'真實LINE條碼照片無文字直接交接',
            'messages':['想了解廈門四天，兩位，12/29到1/1',
                {'text':'[客户发送附件]','attachments':[{'id':103851896,'file_type':'image',
                    'data_url':os.environ['REHEARSAL_LINE_QR_URL']}]}],
            'expected':'handoff','handoff_type':'contact_image','vision':True})
    if os.environ.get('REHEARSAL_ROOM_IMAGE_URL'):
        extra.append({'id':'actual_room_photo','title':'真實房間照片答疑不誤判留資',
            'messages':['想了解廈門住宿',{'text':'這種房間有床嗎？','attachments':[{'id':90003,'file_type':'image',
                'data_url':os.environ['REHEARSAL_ROOM_IMAGE_URL']}]}], 'expected':'reply','vision':True})
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
                for _ in range(case.get('followup_checks', 0)):
                    if not service.state(row).get('next_check_at'):
                        break
                    service.advance_next(row)
                    db.commit()
                    runs = settle(db, row)
                if case.get('resume_message'):
                    service.add_message(db, row, case['resume_message'], f'case-{case["id"]}-resume')
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
            if case.get('opening_count') and len([m for m in delivered if m.get('v3_kind')=='opening']) != case['opening_count']:
                issues.append('opening_missing_or_repeated')
            if case.get('vision') and not any(log.get('vision_image_count',0)>0 for run in runs for log in (run.trace.get('logs') or [])):
                issues.append('image_not_actually_read')
            if case.get('no_introduction_queue') and any(m.get('v3_kind') == 'introduction' for m in reply):
                issues.append('unnecessary_introduction_queue')
            invitations = [m for m in reply if m.get('contact_invitation')]
            if case.get('invitation') and not invitations:
                issues.append('contact_invitation_missing')
            incoming_positions = [i for i, message in enumerate(messages)
                                  if message.get('direction') == 'incoming']
            for turn in case.get('invite_on_turns', []):
                if turn >= len(incoming_positions):
                    issues.append(f'customer_turn_not_reached_{turn}')
                    continue
                start = incoming_positions[turn] + 1
                end = incoming_positions[turn+1] if turn+1 < len(incoming_positions) else len(messages)
                if not any(m.get('contact_invitation') and m.get('status') == 'simulated_delivered'
                           for m in messages[start:end]):
                    issues.append(f'new_need_invitation_missing_turn_{turn}')
            if case.get('invite_on_final_turn') and incoming_positions:
                final_turn = messages[incoming_positions[-1] + 1:]
                if not any(m.get('contact_invitation') and m.get('status') == 'simulated_delivered'
                           for m in final_turn):
                    issues.append('final_turn_contact_invitation_missing')
            if 'no_invitation_after_turn' in case and len(incoming_positions) > case['no_invitation_after_turn']:
                boundary = incoming_positions[case['no_invitation_after_turn']]
                if any(m.get('contact_invitation') and m.get('status') == 'simulated_delivered'
                       for m in messages[boundary + 1:]):
                    issues.append('contact_invitation_after_explicit_refusal')
            if 'no_proactive_after_turn' in case and len(incoming_positions) > case['no_proactive_after_turn']:
                boundary = incoming_positions[case['no_proactive_after_turn']]
                if any(m.get('proactive') and m.get('status') == 'simulated_delivered'
                       for m in messages[boundary + 1:]):
                    issues.append('proactive_message_after_requested_pause')
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
            if case.get('asset') and not any(m.get('asset_key') == case['asset'] for m in delivered):
                issues.append('expected_asset_missing')
            text = '\n'.join(m.get('content', '') for m in reply)
            if any(word in text for word in case.get('unexpected_text', [])):
                issues.append('unexpected_business_or_customer_claim')
            if any(word in text for word in ('China2Go', '西藏', '入藏', '人民幣', '人民币', '申請款', '申请款')):
                issues.append('unrelated_or_unapproved_claim')
            runs = db.scalars(select(AutomationRun).where(AutomationRun.session_id == sid)).all()
            result = {**case, 'session_id': sid, 'actual': actual, 'issues': issues,
                      'package_sha256': hashlib.sha256((PACK / 'route-package.json').read_bytes()).hexdigest(),
                      'configuration_sha256': hashlib.sha256((PACK / 'reception-config.json').read_bytes()).hexdigest(),
                      'system_prompt_sha256': hashlib.sha256((ROOT / 'businesses/rujing2/system-prompt.txt').read_bytes()).hexdigest(),
                      'runtime_sha256': hashlib.sha256((ROOT / 'backend/app/reception_v3/runtime.py').read_bytes()).hexdigest(),
                      'repetition_sha256': hashlib.sha256((ROOT / 'backend/app/reception_v3/repetition.py').read_bytes()).hexdigest(),
                      'common_skill_sha256': hashlib.sha256((ROOT / 'businesses/rujing2/skills/business-reception/SKILL.md').read_bytes()).hexdigest(),
                      'effective_skills_digest': effective_digest,
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
