"""Release regressions: customer constraints, route extension and delivery obligations."""
from copy import deepcopy
from dataclasses import asdict
import pytest
from sqlalchemy import select
from app.customer_contact_policy import contact_constraint
from app.deepseek_evaluation import EvaluationDecision
from app.models import ConversationJourney, ConversationState, HandoffTask
from app.reception_v2.events import validate_events, merge_events, answer_receipt
from app.reception_v2.runtime import _enforce_delivery_contract
from app.route_packages import ROUTES

NOW = '2026-09-20T02:00:00+00:00'
ROUTE = 'peach_9d_2027'


def event(kind, quote, **extra):
    return validate_events([{'type': kind, 'quote': quote, **extra}],
        {'customer_text': quote, 'now': NOW, 'source_message_id': 10})


def test_silence_does_not_replay_last_customer_question_as_new_input():
    from app.reception_v2.runtime import _messages
    from app.reception_v2.skill_registry import SkillRegistry
    messages = _messages({'module':'silence_touch','route_variant':ROUTE,'customer_text':'在哪集合？',
        'context_messages':[{'role':'customer','content':'在哪集合？'},
                            {'role':'assistant','content':'林芝接機。'}]}, SkillRegistry())
    assert len([m for m in messages if m['role'] == 'user' and m['content'] == '在哪集合？']) == 1


def test_hotel_request_preserves_separate_customer_question():
    decision = EvaluationDecision('reply','peach_9d','other',route_variant=ROUTE,
        reply='第一天林芝接機。',evidence_refs=['route.9.arrival'],
        v2_events=[{'type':'material_requested','material_kind':'hotel'},
                   {'type':'question','quote':'在哪集合？','source_message_id':10}])
    keys = ROUTES[ROUTE]['groups']['hotel_reference']['assets']
    _enforce_delivery_contract({'available_materials':[{'key':k} for k in keys]},decision)
    assert decision.v2_delivery_sections[0]['answers_customer_question']
    assert decision.v2_delivery_sections[0]['text'] == '第一天林芝接機。'
    assert decision.v2_delivery_sections[1]['asset_keys']


def test_full_intro_discards_unneeded_model_summary_before_single_message_limit():
    import json
    from app.reception_v2.runtime import _validated_decision
    raw = {'action':'reply','route_variant':ROUTE,'reply':'不應發送的冗長摘要。'*100,
        'v2_events':[{'type':'material_requested','quote':'完整介紹','material_kind':'full_introduction'}]}
    decision = _validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'完整介紹','module':'reply'})
    keys = {k for g in ROUTES[ROUTE]['groups'].values() for k in g['assets']}
    _enforce_delivery_contract({'available_materials':[{'key':k} for k in keys]},decision)
    assert len(decision.v2_delivery_sections) >= 5
    assert all('冗長摘要' not in s['text'] for s in decision.v2_delivery_sections)


def test_reactive_handoff_cannot_skip_answer_verification_with_empty_reply():
    import json
    from app.reception_v2.runtime import _validated_decision
    with pytest.raises(ValueError, match='v2_customer_reply_required'):
        _validated_decision({'content':json.dumps({'action':'handoff','reply':None,'v2_events':[],
            'handoff_reason':'knowledge_confirmation_required'})},set(),set(),{'module':'reply','customer_text':'需要什麼證明？'})


def test_incomplete_party_extraction_does_not_overwrite_date_acknowledgment():
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','route_variant':ROUTE,'reply':'6位，預計3月28日出發。',
         'slots':{'party_size':'6'},'slot_evidence':{'party_size':'6位'},
         'v2_events':[{'type':'profile_updated','quote':'我們6位，3月28日出發','topic':'party_size'}],
         'journey_stage':'value_building'}
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':'reply','customer_text':'我們6位，3月28日出發'})
    assert decision.reply==raw['reply']
    assert decision.slots=={'party_size':'6'}  # The semantic audit must repair the omitted date.


@pytest.mark.parametrize('slots,evidence,error',[
    ({'departure_date':'3月28日'},{'departure_date':'3月28日'},'v2_slot_unknown_field'),
])
def test_profile_parse_never_silently_discards_customer_updates(slots,evidence,error):
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','reply':'已記下日期。','slots':slots,'slot_evidence':evidence,'v2_events':[]}
    decision = _validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'3月28日出發'})
    assert decision.slots == {'departure_window': '3月28日'}


def test_followup_question_cannot_recommit_historical_profile_as_current_input():
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','reply':'依6人報價。','slots':{'party_size':'6'},
         'slot_evidence':{'party_size':'我們6位'},'v2_events':[{'type':'question','quote':'價格多少？'}]}
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':'reply','customer_text':'價格多少？','journey':{'customer_profile':{'party_size':'6'}}})
    assert decision.slots=={} and decision.slot_evidence=={}
    assert decision.reply=='依6人報價。'


@pytest.mark.parametrize('module',['silence_touch','wakeup'])
def test_scheduler_cannot_persist_model_invented_customer_profile(module):
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','reply':'補充一項資訊。','slots':{'party_size':'10','departure_date':'9月'},
         'slot_evidence':{'party_size':'10位'},'v2_events':[]}
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':module,'customer_text':'我們6位','journey':{'customer_profile':{'party_size':'6'}}})
    assert decision.slots=={} and decision.slot_evidence=={}


def test_missing_action_is_repaired_once_instead_of_silently_inferred(monkeypatch):
    import json
    import app.reception_v2.runtime as runtime
    from app.reply_fact_verification import FactVerification
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    calls=[]
    def model(payload,round_index):
        calls.append(payload)
        value={'reply':'收到。','route_variant':ROUTE,'v2_events':[]}
        if len(calls)>1: value['action']='reply'
        return {'content':json.dumps(value)}, {'duration_ms':1,'status':'completed'}
    monkeypatch.setattr(runtime,'_call',model)
    result,_,_,_=runtime.run_v2_agent({'module':'reply','customer_text':'好','route_variant':ROUTE})
    assert result.action=='reply' and len(calls)==2


def test_appointment_without_contact_identifier_is_not_captured():
    import json
    from app.reception_v2.runtime import _validated_decision
    decision = _validated_decision({'content':json.dumps({'action':'handoff','route_variant':ROUTE,
        'reply':'好的，明天再聯繫。','lead_action':'captured','handoff_reason':'lead_captured',
        'v2_events':[{'type':'contact_agreed','quote':'明天十点联系',
                      'contact_at':'2026-09-21T10:00:00+08:00'}]})},set(),set(),
        {'module':'reply','customer_text':'明天十点联系','now':NOW})
    assert decision.lead_action=='none' and decision.action=='reply'
    assert decision.v2_events[0]['type']=='contact_scheduled'


def test_missing_event_field_is_not_silently_treated_as_no_customer_request():
    import json
    from app.reception_v2.runtime import _validated_decision
    with pytest.raises(ValueError,match='v2_invalid_events'):
        _validated_decision({'content':json.dumps({'action':'reply','reply':'您好'})},set(),set(),{})


def test_plain_itinerary_request_compiles_caption_before_model_length_check():
    import json
    from app.reception_v2.runtime import _validated_decision
    raw={'action':'reply','route_variant':ROUTE,'reply':'冗長模型摘要'*100,
         'v2_events':[{'type':'material_requested','quote':'給我行程圖','material_kind':'itinerary'}]}
    decision=_validated_decision({'content':json.dumps(raw)},set(),set(),
        {'module':'reply','customer_text':'給我行程圖'})
    _enforce_delivery_contract({'available_materials':[{'key':'routes12-9d-itinerary'}]},decision)
    assert decision.reply==ROUTES[ROUTE]['groups']['itinerary_overview']['text']
    assert decision.material_keys==['routes12-9d-itinerary']


def test_new_question_clears_considering_but_preserves_explicit_time():
    slots = merge_events({}, event('considering', '先考虑一下'))
    slots = merge_events(slots, event('contact_agreed', '下午联系', contact_at='2026-09-20T08:00:00+00:00'))
    slots = merge_events(slots, event('question', '在哪里集合'))
    state = slots['_v2_state']
    assert 'reevaluate_at' not in state and 'waiting_reason' not in state
    assert contact_constraint({'memory': slots, 'now': NOW}) == ('customer_requested_time', 360)


def test_new_question_does_not_revoke_global_optout_but_explicit_agreement_does():
    slots = merge_events({}, event('contact_refused', '不要联系', scope='all'))
    slots = merge_events(slots, event('question', '在哪里集合'))
    assert contact_constraint({'memory': slots, 'now': NOW}) == ('customer_opted_out', 0)
    slots = merge_events(slots, event('contact_agreed', '还是下午联系我', contact_at='2026-09-20T08:00:00+00:00'))
    assert contact_constraint({'memory': slots, 'now': NOW}) == ('customer_requested_time', 360)


@pytest.mark.parametrize('target', ['v1','v2'])
def test_engine_projection_drops_inferred_terminal_stage_but_keeps_customer_evidence(session_factory, target):
    from test_reception_engine_switch import _seed_state
    from app.reception_v2.engine_projection import project_engine_state
    sid = _seed_state(session_factory)
    with session_factory() as db:
        state = db.get(ConversationState, sid)
        slots = merge_events({'party_size': 6}, event('contact_refused', '不留LINE', scope='LINE'))
        journey = ConversationJourney(conversation_state_id=sid, route_variant=ROUTE, stage='captured', slots=slots)
        db.add(journey)
        db.flush()
        project_engine_state(db, state, 'v2' if target == 'v1' else 'v1', target, NOW)
        db.commit()
        db.expire_all()
        assert journey.stage == 'value_building'
        assert journey.slots['party_size'] == 6
        assert journey.slots['_v2_state']['refused_channels'] == ['LINE']


def test_projection_preserves_real_handoff(session_factory):
    from test_reception_engine_switch import _seed_state
    from app.reception_v2.engine_projection import project_engine_state
    sid = _seed_state(session_factory)
    with session_factory() as db:
        state = db.get(ConversationState, sid)
        journey = ConversationJourney(conversation_state_id=sid, route_variant=ROUTE, stage='value_building')
        db.add_all([journey, HandoffTask(conversation_state_id=sid, reason_code='requested_material_unavailable', status='pending')])
        db.flush()
        project_engine_state(db, state, 'v2', 'v1', NOW)
        assert journey.stage == 'handoff'


@pytest.mark.parametrize('kind', ['itinerary','full_introduction','hotel','vehicle','altitude'])
def test_missing_requested_material_is_actionable_handoff_without_false_receipt(kind):
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='other', route_variant=ROUTE,
        reply='資料給您', v2_events=event('material_requested', '给我资料', material_kind=kind))
    _enforce_delivery_contract({'available_materials': []}, decision)
    assert decision.action == 'handoff' and decision.handoff_reason == 'requested_material_unavailable'
    assert not decision.material_keys and not decision.v2_delivery_sections
    assert answer_receipt(asdict(decision), decision.reply) is None


def test_future_contact_outside_channel_window_is_handoff_with_original_date():
    when = '2026-09-22T08:00:00+08:00'
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='other', route_variant=ROUTE,
        reply='好的', v2_events=event('contact_agreed', '后天早上八点联系', contact_at=when))
    _enforce_delivery_contract({'now': NOW, 'last_customer_at': NOW}, decision)
    assert decision.action == 'handoff' and decision.handoff_reason == 'customer_contact_outside_window'
    assert decision.v2_events[0]['contact_at'] == when


def test_full_introduction_keeps_extra_answer_and_receipts_are_independent():
    questions = event('question', '小费多少')
    decision = EvaluationDecision(action='reply', branch='peach_9d', intent='other', route_variant=ROUTE,
        reply='小費每人每天30元。', evidence_refs=['service.tips'], slots={'party_size': 6},
        v2_events=[*questions, *event('material_requested', '给完整介绍', material_kind='full_introduction')])
    materials = [{'key': key} for group in ROUTES[ROUTE]['groups'].values() for key in group['assets']]
    _enforce_delivery_contract({'available_materials': materials}, decision)
    sections = decision.v2_delivery_sections
    assert sections[-1]['answers_customer_question']
    assert all(s['group_key'] != 'party_question' for s in sections)
    assert answer_receipt(asdict(decision), sections[-1]['text'])['questions'] == questions
    assert answer_receipt(asdict(decision), sections[0]['text'])['questions'] == []


def test_material_retrieval_does_not_hide_assets_when_keywords_do_not_match():
    from app.reception_v2.tools import execute_tool
    from app.reception_v2.skill_registry import SkillRegistry
    result = execute_tool('get_route_materials', {'route_variant': ROUTE, 'topic': '让我看看长什么样'}, SkillRegistry())
    assert {m['key'] for m in result['materials']} == {
        key for group in ROUTES[ROUTE]['groups'].values() for key in group['assets']}


def test_third_route_registers_followup_candidates_without_editing_runtime(tmp_path, monkeypatch):
    from app.reception_v2 import route_profiles, skill_registry, flow_classifier
    root = tmp_path / 'skills'
    root.mkdir()
    (root / 'SKILL.md').write_text('---\nname: island-5d\ndescription: 海岛五日\nroute_variant: island_5d\nfollowup_groups: island\nroute_aliases: 海岛五日|5日海岛\n---\n只使用海岛批准事实。', encoding='utf-8')
    registry = skill_registry.SkillRegistry(root)
    spec = deepcopy(ROUTES[ROUTE])
    spec.update(name='海岛五日', branch='island_5d', groups={'island': {
        'text': '珊瑚岛风光', 'purpose': '自然景观', 'assets': [], 'evidence': ['island.nature']}})
    monkeypatch.setitem(ROUTES, 'island_5d', spec)
    monkeypatch.setattr(skill_registry, 'SkillRegistry', lambda: registry)
    monkeypatch.setattr(flow_classifier, 'SkillRegistry', lambda: registry)
    profile = route_profiles.route_profile('island_5d')
    assert profile['skill'] == 'island-5d'
    assert profile['followup_candidates'] == ('island.nature',)
    assert flow_classifier.infer_route_variant('我想参加海岛五日') == 'island_5d'
    import json
    from app.reception_v2.runtime import _validated_decision
    decision = _validated_decision({'content':json.dumps({'action':'reply','route_variant':'island_5d','v2_events':[],
        'reply':'這條行程可以欣賞珊瑚島風光。','evidence_refs':['island.nature']})},
        {'island.nature'},set(),{'module':'reply','customer_text':'介紹一下海島五日'})
    assert decision.branch == 'island_5d' and decision.route_variant == 'island_5d'


def test_published_opening_is_frozen_as_delivery_items():
    from app.reception_v2.runtime import _attach_configured_opening
    from app.deepseek_evaluation import EvaluationDecision
    decision = EvaluationDecision('reply','unclassified','other',reply='greeting')
    _attach_configured_opening({'module': 'reply', 'context_messages': [], 'reception_policy': {
        'operator_configuration': {'opening_messages': ['歡迎來諮詢。', '您想了解哪條行程？'], 'opening_interval_seconds': 2}}}, decision)
    assert decision.opening_messages == ['歡迎來諮詢。', '您想了解哪條行程？']
    assert len(decision.reply_options) == len(ROUTES)


def test_deferred_sandbox_job_creates_new_execution_at_next_due_time(session_factory, monkeypatch):
    from test_playground_journey import setup_journey, bind_new_route_snapshot
    from app.automation_models import RehearsalEnrollment, RehearsalJob, AutomationRun
    from app.automation_service import enroll_rehearsal, advance_sops, process_automation_run
    from app.reception_v2 import ENGINE_RELEASE_ID
    with session_factory() as db:
        session, version = setup_journey(db)
        bind_new_route_snapshot(session)
        session.engine_version, session.engine_release_id = 'v2', ENGINE_RELEASE_ID
        session.due_at = None
        for old in db.scalars(select(RehearsalEnrollment)):
            old.status = 'completed'
        db.flush()
        enrollment = enroll_rehearsal(db, session, version, source='model_route', reenroll=True,
            request_key='defer-integration', schedule_intervals=[1])
        job = db.scalar(select(RehearsalJob).where(RehearsalJob.enrollment_id == enrollment.id))
        session.virtual_now = job.scheduled_at
        advance_sops(db, session); db.commit()
        first = db.scalar(select(AutomationRun).where(AutomationRun.module == 'silence_touch'))
        assert first
        decision = EvaluationDecision(action='no_action', branch='peach_9d', intent='other',
            route_variant=ROUTE, journey_stage='value_building', wakeup_action='defer', defer_minutes=5)
        monkeypatch.setattr('app.automation_service.generate_decision', lambda _: (decision, [], '', {}))
        assert process_automation_run(db, environment='playground')
        db.refresh(job)
        assert job.status == 'scheduled'
        session.virtual_now = job.scheduled_at
        advance_sops(db, session); db.commit()
        runs = db.scalars(select(AutomationRun).where(AutomationRun.module == 'silence_touch')).all()
        assert len(runs) == 2
        assert len({r.idempotency_key for r in runs}) == 2


@pytest.mark.parametrize('engine', ['v1','v2'])
def test_sandbox_static_proactive_gate_obeys_persisted_customer_optout(session_factory, engine):
    from test_playground_journey import setup_journey
    from app.automation_service import gate
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.engine_version = engine
        session.due_at = None
        session.controls = {**session.controls, 'journey': {'slots': {'_v2_state': {'proactive_opt_out': True}}}}
        assert gate(session, session.virtual_now, True, db) == 'customer_opted_out'
        assert gate(session, session.virtual_now, False, db) is None


@pytest.mark.parametrize('available', [False, True])
def test_captured_contact_attaches_only_available_guide_or_records_specific_pending_work(available):
    decision = EvaluationDecision(action='handoff', branch='peach_9d', intent='contact', route_variant=ROUTE,
        reply='收到', lead_action='captured', contact_values={'wechat':'test_travel'}, handoff_reason='lead_captured')
    _enforce_delivery_contract({'available_materials': [{'key':'china2go-altitude-guide-v1'}] if available else []}, decision)
    assert decision.action == 'handoff' and decision.lead_action == 'captured'
    if available:
        assert decision.material_keys == ['china2go-altitude-guide-v1']
        assert decision.covered_content_groups == ['altitude_guide']
    else:
        assert not decision.material_keys
        assert 'pending_material:altitude_guide' in decision.safety_flags
        assert '補給' in decision.reply


def test_pdf_replacement_requires_new_approval_and_cannot_inherit_image_approval(session_factory, tmp_path):
    import hashlib
    from app.material_library import replace_asset_binding, candidate_materials
    from app.models import KnowledgeVersion, MaterialAsset, StoredMedia
    from app.route_packages import ROUTE_PACKAGES
    with session_factory() as db:
        version = KnowledgeVersion(tenant_id=1,version_key=ROUTE_PACKAGES[ROUTE]['knowledge_version'],title='test',content_hash='x')
        db.add(version); db.flush()
        path=tmp_path/'approved.pdf'; path.write_bytes(b'%PDF-1.4\nfixture old')
        media=StoredMedia(tenant_id=1,created_by=1,original_name=path.name,media_type='file',mime_type='application/pdf',file_size=path.stat().st_size,storage_path=str(path))
        db.add(media); db.flush()
        asset=MaterialAsset(knowledge_version_id=version.id,asset_key='china2go-altitude-guide-v1',source_path=str(path),
            display_name='guide',media_type='file',available=True,file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            metadata_json={'stored_media_id':media.id,'review_state':'evaluation_ready','live_approved':True,'route_variants':[ROUTE]})
        db.add(asset); db.commit()
        newpath=tmp_path/'replacement.pdf'; newpath.write_bytes(b'%PDF-1.4\nchanged content')
        replacement=StoredMedia(tenant_id=1,created_by=1,original_name=newpath.name,media_type='file',mime_type='application/pdf',file_size=newpath.stat().st_size,storage_path=str(newpath))
        db.add(replacement); db.flush()
        replace_asset_binding(db,asset,replacement)
        db.commit()
        assert asset.metadata_json['live_approved'] is False
        assert asset.metadata_json['review_state'] == 'pending'
        assert candidate_materials(db,1) == []
