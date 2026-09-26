import pytest


@pytest.mark.parametrize('text,expected',[
    ('若您有日期，我可以再幫您確認。',True),
    ('我幫您核對這6人的適用優惠。',True),
    ('顧問會按您的證件核對。',True),
    ('不用請顧問確認。',False),
    ('我不能幫您確認醫療風險。',False),
    ('我不會再幫您查詢。',False),
    ('請您先跟家人確認日期。',False),
    ('好的，4位已記下。',False),
    ('我這邊先幫您查詢票價。',True),
    ('您確認後再告訴我。',False),
    ('若您想9月走西藏，我再幫您看看其他安排。',True),
    ('我會幫您找找合適的行程。',True),
    ('我先給您看看行程圖。',False),
])
def test_checking_action_candidates(text,expected):
    from app.reception_v2.reply_scope import checking_commitment
    assert checking_commitment(text)==expected


@pytest.mark.parametrize('needed',[False,True])
def test_first_person_task_omitted_by_scope_still_requires_independent_proof(monkeypatch,needed):
    from app.reception_v2.reply_scope import verify_scope
    body='我們主打4至10人小團。若您有日期，我可以再幫您確認。'
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:({
        'event_errors':[],'event_error_checks':[],'question_checks':[],
        'contact_refusals':[],'material_request_checks':[],'consultant_task_checks':[]},[],''))
    calls=[]
    def proof(data,tasks):
        calls.append((data,tasks))
        return ([{'index':0,'needed':needed,'remove_from_reply':not needed,
                  'required_task':'核對所問日期人數' if needed else '', 'has_unasked_details':False}],[],'proof')
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',proof)
    result=verify_scope({'customer_message':'請查這天人數' if needed else '通常幾位旅客一起出發？','proposed_body':body})[0]
    assert len(calls)==1 and '我可以再幫您確認' in calls[0][1][0]
    assert bool(result['task_scope_errors']) is not needed
    assert ('我可以再幫您確認。' in result.get('unwanted_parts',[])) is not needed


@pytest.mark.parametrize('text,expected',[
    ('我這邊沒有公布折扣金額。',True),
    ('我这边尚未公布。',True),
    ('公布的出發日期是3月20日至4月10日。',False),
])
def test_internal_unpublished_voice(text,expected):
    from app.advisor_voice import v2_internal_copy_violation
    assert bool(v2_internal_copy_violation(text))==expected


def test_fact_repair_has_explicit_empty_task_permission():
    from app.reception_v2.reply_revision import scope_revision_context
    from app.reply_fact_verification import FactVerification
    result=scope_revision_context(FactVerification(False,scope_check={'current_request':'需要什麼證明？'}))
    assert result['consultant_tasks']==[] and result['consultant_policy_explanations']==[]


@pytest.mark.parametrize('material,route_matches',[(False,True),(True,True),(False,False)])
def test_profile_receipt_preserves_valid_route_selection(material,route_matches):
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    from app.reception_v2.reply_revision import verified_profile_ack
    events=[{'type':'route_selected','quote':'我們6位想去9日','route_variant':'peach_9d_2027' if route_matches else 'peach_11d_2027'},
            {'type':'profile_updated','quote':'我們6位想去9日'}]
    scope={'profile_ack_only':True,'profile_update_checks':[{'field':'party_size','value':'6','persisted_correctly':True}]}
    if material:
        events.append({'type':'material_requested','quote':'給我圖','material_kind':'itinerary'})
        scope['requested_material_kinds']=['itinerary']
    decision=EvaluationDecision('reply','peach_9d','other',route_variant='peach_9d_2027',slots={'party_size':'6'},v2_events=events)
    result=verified_profile_ack(decision,FactVerification(True,scope_check=scope))
    if material or not route_matches:
        assert result is None
    else:
        assert '桃花9日' in result[0]['reply'] and '同行6位' in result[0]['reply']
        assert result[0]['v2_events']==events


def test_elliptical_question_reference_is_repairable_but_never_trusted(monkeypatch):
    from copy import deepcopy
    from app.reception_v2.reply_scope import verify_scope
    value={'event_error_checks':[],'question_checks':[{'request_quote':'戶外氧氣要自己準備嗎？',
        'request_kind':'fact','answer_kind':'text','answer_segment_ids':['reply_0'],'answer_asset_ids':[],'covered':True}],
        'contact_refusals':[],'material_request_checks':[],'consultant_task_checks':[],
        'profile_update_checks':[],'traveler_age_checks':[],'unwanted_part_checks':[],
        'current_request':'戶外氧氣','required_answer':'需要核對供應','requested_material_kinds':[]}
    def model(**kw):
        with pytest.raises(ValueError,match='question_evidence_invalid'):
            kw['parser'](deepcopy(value))
        fixed=deepcopy(value)
        fixed['question_checks'][0]['request_quote']='自己要準備嗎？'
        return kw['parser'](fixed),[],''
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    result=verify_scope({'customer_message':'自己要準備嗎？','proposed_body':'實際供應需要核對。'})[0]
    assert result['question_checks'][0]['request_quote']=='自己要準備嗎？'


def test_scope_cannot_invent_extra_work_from_reference_facts(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    quote='這趟具體供應請顧問核對。'
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:({
        'event_errors':[],'event_error_checks':[],'question_checks':[],'contact_refusals':[],
        'material_request_checks':[],'consultant_task_checks':[{'task':'核對供應及租用費用',
            'draft_quote':quote,'needed':True}]},[],''))
    def proof(data,tasks):
        assert tasks==[quote]
        return ([{'index':0,'needed':True,'remove_from_reply':False,
                  'required_task':'核對具體供應','has_unasked_details':False}],[],'proof')
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',proof)
    result=verify_scope({'customer_message':'每天戶外都有氧氣嗎？','proposed_body':quote})[0]
    assert result['task_scope_errors']==[]
