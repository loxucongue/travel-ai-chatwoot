"""Candidate16 failures: independently grounded repairs and actual delivery."""
from types import SimpleNamespace
import pytest
from app.reception_v2.reply_scope import verify_scope
from app.reception_v2.state_revision import revise_grounded_state


@pytest.mark.parametrize('kind,segments,assets,missing',[
    ('text',['reply_0'],[],False),('text',['fact.culture'],[],True),
    ('attachment',[],['itinerary'],False),('attachment',[],['invented'],True),
    ('text',[],['itinerary'],True),
])
def test_actual_response_ids_cannot_be_replaced_by_fact_ids(monkeypatch,kind,segments,assets,missing):
    result={'contact_refusals':[], 'question_checks':[{'request_quote':'介紹',
        'request_kind':'material' if kind=='attachment' else 'fact','answer_kind':kind,'answer_segment_ids':segments,'answer_asset_ids':assets,'covered':True}]}
    def model(**kw):
        assert kw['input_data']['actual_answer_segments']==[{'id':'reply_0','text':'政教文化介紹。'}]
        return result,[],''
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    actual=verify_scope({'customer_message':'介紹','proposed_body':'政教文化介紹。',
        'selected_asset_ids':['itinerary'],'ordered_delivery_sections':[{'text':'政教文化介紹。'}],
        'v2_events':[{'type':'material_requested'}]})[0]
    assert bool(actual.get('missing_answers'))==missing
    assert bool(actual.get('event_errors'))==(kind=='text')


def test_profile_repair_keeps_copy_and_other_events_exactly():
    original={'reply':'可以先了解行程。','reply_body':'可以先了解行程。',
        'slots':{},'slot_evidence':{},'v2_events':[{'type':'question','quote':'能先了解嗎'}],
        'material_keys':[], 'evidence_refs':['route.example']}
    audit=SimpleNamespace(scope_check={'profile_update_checks':[{'field':'departure_window',
        'quote':'日期還沒定','value':'未定','persisted_correctly':False}]})
    revised=revise_grounded_state({'customer_text':'日期還沒定，能先了解嗎'},original,None,audit)
    assert revised['reply']==original['reply'] and revised['reply_body']==original['reply_body']
    assert revised['slots']=={'departure_window':'未定'}
    assert revised['v2_events'][0]==original['v2_events'][0]
    assert original['slots']=={}  # No mutation of the rejected evidence.
    assert revise_grounded_state({'customer_text':'價格多少'},original,None,audit) is None
    assert revise_grounded_state({'module':'silence_touch'},original,None,audit) is None


def test_display_conversion_preserves_approval_and_document_meanings():
    from app.advisor_voice import normalize_v2_customer_copy
    assert normalize_v2_customer_copy('审批通过后领取纸质入藏文件。')=='審批通過後領取紙質入藏文件。'


def test_empty_metadata_cleanup_preserves_actual_unknown_and_policy():
    from app.advisor_voice import normalize_v2_customer_copy
    assert normalize_v2_customer_copy('證明要求需核對，這點資料沒有明確，我請顧問確認。')=='證明要求需核對，我請顧問確認。'
    assert normalize_v2_customer_copy('資料沒有明確證明您可免交健康文件。')=='資料沒有明確證明您可免交健康文件。'


@pytest.mark.parametrize('needed,complete,remove',[(True,False,False),(False,False,False),(False,True,True)])
def test_task_removal_is_derived_from_minimum_answer(monkeypatch,needed,complete,remove):
    from app.reception_v2.task_scope import verify_task_scope
    monkeypatch.setattr('app.reception_v2.task_scope.call_json_node',lambda **kw:(kw['parser']({
        'checks':[{'index':0,'reason':'independent necessity','needed':needed,
                   'answer_complete_without_task':complete,'required_task':'checking' if needed else '',
                   'has_unasked_details':False}]}),[],''))
    assert verify_task_scope({},['checking'])[0][0]['remove_from_reply']==remove


def test_actual_age_is_not_a_policy_threshold(monkeypatch):
    from app.reception_v2 import runtime
    from app.reply_fact_verification import FactVerification
    from app.deepseek_evaluation import EvaluationDecision
    audit=FactVerification(True,scope_check={'traveler_age_checks':[{'age':64,'taiwan_traveler':True}]})
    monkeypatch.setattr(runtime,'call_reply_fact_verifier',lambda *a:(audit,[],''))
    body='64歲不會因為未滿65歲而不能報名。'
    decision=EvaluationDecision('reply','peach_9d','other',reply=body,reply_body=body,
        route_variant='peach_9d_2027',evidence_refs=['service.peach_age'])
    result=runtime._verify({'customer_text':'台灣人64歲，最低65嗎？'},decision)[0]
    assert not any('健康证明条件' in v for v in result.contract_violations)


@pytest.mark.parametrize('body,blocked',[
    ('臺灣65至75歲需健康證明；您64歲這部分不用。',True),
    ('您不用交健康證明。',True),
    ('不能說您不用交健康證明。',False),
    ('65歲不是最低年齡，64歲不會因未滿65而不能報名。',False),
    ('健康證明是否需要，請顧問按證件核對。',False),
])
def test_certificate_waiver_guard_preserves_unknown_and_negation(body,blocked):
    from app.reception_v2.claim_guards import unsupported_certificate_waiver
    assert unsupported_certificate_waiver(body)==blocked


@pytest.mark.parametrize('body,expected',[
    ('珠峰住絨布，其他地區安排希爾頓。',['波密']),
    ('波密除外，其他地區安排希爾頓。',['珠峰']),
    ('波密和珠峰除外，其他地區安排希爾頓。',[]),
    ('珠峰住絨布旅館，客房有獨立衛浴。',[]),
])
def test_unqualified_other_regions_does_not_drop_hotel_exceptions(body,expected):
    from app.reception_v2.claim_guards import missing_hotel_exceptions
    assert missing_hotel_exceptions(body,'11日住宿除波密與珠峰段外，其餘安排希爾頓。')==expected


@pytest.mark.parametrize('has_text_question',[False,True])
def test_material_recompile_preserves_real_composite_question(has_text_question):
    events=[{'type':'material_requested','quote':'完整介紹','material_kind':'full_introduction'},
            {'type':'question','quote':'完整介紹'}]
    decision=SimpleNamespace(v2_events=events,action='reply',lead_action='none',handoff_reason=None)
    scope={'material_request_checks':[{'kind':'full_introduction','quote':'完整介紹'}],
           'question_checks':[{'answer_kind':'text','request_quote':'小費多少'}] if has_text_question else []}
    result=revise_grounded_state({'customer_text':'完整介紹'}, {'v2_events':events},decision,SimpleNamespace(scope_check=scope))
    assert (result is None)==has_text_question
    if result: assert [e['type'] for e in result['v2_events']]==['material_requested']


def test_verified_profile_receipt_discards_stale_question_and_material(monkeypatch):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import revise_copy
    decision=EvaluationDecision('reply','peach_9d','other',reply='4位。再介紹行程。',
        slots={'party_size':'4'},slot_evidence={'party_size':'我們4位'},material_keys=['old-itinerary'],
        v2_events=[{'type':'profile_updated','quote':'我們4位'},{'type':'question','quote':'我們4位'}])
    audit=FactVerification(True,scope_check={'profile_ack_only':True,
        'profile_update_checks':[{'field':'party_size','persisted_correctly':True}]})
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('No new copy generation'))
    result,_,_=revise_copy({},decision,audit,set())
    assert result['reply']=='好喔，同行4位。'
    assert result['material_keys']==[] and [e['type'] for e in result['v2_events']]==['profile_updated']
