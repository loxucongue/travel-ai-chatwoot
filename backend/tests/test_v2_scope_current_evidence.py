import pytest

from app.reception_v2.reply_scope import verify_scope


def audit(monkeypatch, data, profiles=None, materials=None):
    def model(**kwargs):
        return {'profile_update_checks': profiles or [],
                'material_request_checks': materials or [],
                'requested_material_kinds': ['itinerary'],
                'contact_refusals': [], 'event_errors': []}, [], 'digest'
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node', model)
    return verify_scope(data)[0]


def test_price_followup_does_not_recommit_historical_party_and_date(monkeypatch):
    result=audit(monkeypatch, {'customer_message':'價格多少？',
        'recent_conversation':[{'role':'customer','content':'我們6位，3月28日出發'}],
        'current_profile_updates':{'slots':{},'evidence':{}}}, profiles=[
            {'field':'party_size','quote':'我們6位','persisted_correctly':False},
            {'field':'departure_window','quote':'3月28日','persisted_correctly':False}])
    assert result['profile_update_checks']==[]
    assert result['event_errors']==[]


def test_historical_check_cannot_authorize_new_profile_write(monkeypatch):
    result=audit(monkeypatch, {'customer_message':'價格多少？',
        'recent_conversation':[{'role':'customer','content':'我們6位'}],
        'current_profile_updates':{'slots':{'party_size':'6'},'evidence':{'party_size':'我們6位'}}},
        profiles=[{'field':'party_size','quote':'我們6位','persisted_correctly':True}])
    assert len(result['event_errors'])==1


@pytest.mark.parametrize('current,expected',[('我們4位',[]),('行程圖再發一次',['itinerary'])])
def test_material_event_requires_current_request_even_after_delivery(monkeypatch,current,expected):
    quote='行程圖再發一次' if expected else '請發行程图'
    result=audit(monkeypatch, {'customer_message':current,
        'recent_conversation':[{'role':'customer','content':'請發行程图'}],
        'delivered_materials':{'asset_keys':['routes12-9d-itinerary']}},
        materials=[{'kind':'itinerary','quote':quote}])
    assert result['requested_material_kinds']==expected
    assert bool(result['event_errors'])==bool(expected)


@pytest.mark.parametrize('kind',['profile','material'])
def test_invented_evidence_still_fails_closed(monkeypatch,kind):
    with pytest.raises(ValueError,match='evidence_invalid'):
        audit(monkeypatch, {'customer_message':'價格多少？'},
            profiles=[{'field':'party_size','quote':'我們6位','persisted_correctly':True}] if kind=='profile' else [],
            materials=[{'kind':'itinerary','quote':'請發行程图'}] if kind=='material' else [])


@pytest.mark.parametrize('needed',[False,True])
def test_actual_checking_clause_is_audited_even_when_scope_omits_task(monkeypatch,needed):
    tail='實際每團的報名人數要請顧問幫您確認喔。'
    def proof(data,tasks):
        assert tasks==[tail]
        return [{'index':0,'needed':needed,'reason':'Current customer demand proof'}],[],'proof'
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',proof)
    result=audit(monkeypatch,{'customer_message':'通常幾位旅客一起出發？',
        'proposed_body':'我們主打4至10人小團。'+tail})
    assert result['consultant_tasks']==([tail] if needed else [])
    assert result.get('unwanted_parts',[])==([] if needed else [tail])


def test_negated_consultant_check_is_not_an_execution_promise(monkeypatch):
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',
                        lambda *a:pytest.fail('Negated task must not become a commitment'))
    result=audit(monkeypatch,{'customer_message':'需要核對嗎？',
        'proposed_body':'已公布的團型不需要顧問確認。'})
    assert not result.get('task_scope_checks')


def test_contact_purpose_description_is_not_a_new_task(monkeypatch):
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',
        lambda *a:([{'index':0,'needed':False,'remove_from_reply':False,
                     'reason':'The customer asks the purpose of giving contact details.'}],[],'proof'))
    result=audit(monkeypatch,{'customer_message':'留微信是不是就要付款？',
        'proposed_body':'微信只是方便顧問後續聯絡、核對需求用的，留下聯絡方式不等於報名、付款或簽約。'})
    assert not result.get('consultant_tasks') and not result.get('task_scope_errors')
    assert result.get('consultant_policy_explanations')


def test_unasked_check_does_not_remove_preceding_certificate_condition(monkeypatch):
    body='台灣旅客這個年齡層須提交健康證明，實際文件由顧問協助確認。'
    def proof(data,tasks):
        assert tasks==[body]
        return [{'index':0,'needed':False,'reason':'Certificate condition is already published'}],[],'proof'
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',proof)
    result=audit(monkeypatch,{'customer_message':'台灣人剛滿75歲能參加嗎？','proposed_body':body})
    assert result['unwanted_parts']==['實際文件由顧問協助確認。']


@pytest.mark.parametrize('lead,reason,pending,body_suffix,protected',[
    ('captured','lead_captured',['altitude_guide'],'',True),
    ('none','lead_captured',['altitude_guide'],'',False),
    ('captured','explicit_human_request',['altitude_guide'],'',False),
    ('captured','lead_captured',[],'',False),
    ('captured','lead_captured',['altitude_guide'],'另外贈送禮物。',False),
])
def test_only_compiled_pending_material_receipt_is_protected(monkeypatch,lead,reason,pending,body_suffix,protected):
    quote='並補給您高原行前資料'
    def model(**kwargs):
        return {'profile_update_checks':[], 'material_request_checks':[],
                'contact_refusals':[], 'event_errors':[], 'requested_material_kinds':[],
                'unwanted_parts':[quote], 'unwanted_part_checks':[
                    {'quote':quote,'kind':'unrequested_task','reason':'Model ignored existing obligation'}]},[],'audit'
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    result=verify_scope({'customer_message':'微信是test_traveller26',
        'proposed_body':'聯絡方式已收到，我會請顧問接續協助，並補給您高原行前資料。'+body_suffix,
        'planned_system_action':{'lead_action':lead,'handoff_reason':reason,'pending_materials':pending}})[0]
    assert result['unwanted_parts']==([] if protected else [quote])
