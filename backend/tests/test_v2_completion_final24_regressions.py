import pytest
from copy import deepcopy


@pytest.mark.parametrize('body,missing',[
    ('9,980元起／人的團費不包含機票。',True),
    ('9日6人小團人民幣９，９８０元／人，不包含機票。',False),
    ('9日六人小團9980元，不包含機票。',False),
    ('顧問可協助代訂，團費不含機票。',False),
    ('11日11480元／人。',False),
])
def test_reviewed_price_conditions_are_data_driven(body,missing):
    from app.fact_conditions import missing_answer_conditions
    from app.route_packages import ROUTES
    fact=next(f for f in ROUTES['peach_9d_2027']['knowledge_facts'] if f['id']=='route.9.price')
    assert bool(missing_answer_conditions(body,fact)) is missing


def test_route_package_rejects_malformed_answer_conditions():
    from app.route_packages import _validate,RoutePackageError,load_route_packages
    from pathlib import Path
    package=deepcopy(load_route_packages()['peach_9d_2027'])
    package['knowledge_facts'][0]['answer_conditions']=[{'label':'bad','when_any_of':['9980'],'require_any_of':[]}]
    with pytest.raises(RoutePackageError,match='fact_conditions_invalid'):
        _validate(package,Path('test'))


def test_repeated_middle_clause_preserves_hotel_exceptions_and_new_value(monkeypatch):
    from app.reception_v2.reply_revision import prune_copy
    from app.reply_fact_verification import FactVerification
    from app.deepseek_evaluation import EvaluationDecision
    prefix='波密和珠峰段是希爾頓升級的例外'
    tail='其餘地區安排國際品牌希爾頓飯店'
    repeated='珠峰住絨布旅館；'
    body=prefix+'，'+repeated+tail+'。'
    ref='route.shared.hotel_reference'
    d=EvaluationDecision('reply','peach_11d','other',reply=body,evidence_refs=[ref])
    a=FactVerification(True,claim_checks=[{'claim':text,'supported':True,'evidence':ref} for text in [prefix,tail]],
        scope_check={'unwanted_parts':[repeated],
            'unwanted_part_checks':[{'quote':repeated,'kind':'repeat_content','repeat_of_segment_id':'history_5'}]})
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('Prune cannot generate'))
    result=prune_copy({'module':'silence_touch'},d,a,{ref})
    assert result is not None
    assert result[0]['reply']==prefix+'；'+tail+'。'

@pytest.mark.parametrize('kind,rechecks',[('unrequested_topic',False),('unrequested_task',False),('unnecessary_question',True)])
def test_unambiguous_extra_copy_goes_to_repair_without_adjudication(monkeypatch,kind,rechecks):
    from app.reception_v2.reply_scope import verify_scope
    base={'event_error_checks':[],'question_checks':[],'contact_refusals':[],
        'requested_material_kinds':[],'consultant_task_checks':[],
        'unwanted_parts':['另外要看线路吗？'],
        'unwanted_part_checks':[{'quote':'另外要看线路吗？','kind':kind}]}
    calls=[]
    def model(**kw):
        calls.append(kw['node'])
        return deepcopy(base),[],str(len(calls))
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',model)
    result,_,_=verify_scope({'customer_message':'公司特色？','proposed_body':'小团。另​​外要看线路吗？'.replace('​​','')})
    assert result['unwanted_parts']==base['unwanted_parts']
    assert ('v2_scope_coverage_recheck' in calls) is rechecks


def test_trailing_prune_cannot_leave_unproved_dependent_prefix(monkeypatch):
    from app.reception_v2.reply_revision import prune_copy
    from app.reply_fact_verification import FactVerification
    from app.deepseek_evaluation import EvaluationDecision
    body='我們主打4至10人小團。人數不同，實際車型與報價會由顧問依您的需求確認。'
    d=EvaluationDecision('reply','peach_9d','other',reply=body,evidence_refs=['route.shared.brand_positioning'])
    a=FactVerification(True,claim_checks=[{'claim':'我們主打4至10人小團','supported':True,'evidence':'route.shared.brand_positioning'},
        {'claim':'人數不同，實際車型與報價會由顧問依您的需求確認','supported':True,'evidence':'route.shared.brand_positioning'}],
        scope_check={'unwanted_parts':['實際車型與報價會由顧問依您的需求確認。']})
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('Prune cannot generate'))
    assert prune_copy({},d,a,set(d.evidence_refs)) is None


def test_deadline_error_keeps_current_turn_trace_and_resets_context():
    from app.reception_v2.budget import bounded_turn,turn_trace
    from app.deepseek_evaluation import EvaluationCallError
    @bounded_turn
    def fail():
        turn_trace.get().append({'node':'test_completed','duration_ms':23000})
        raise TimeoutError('late')
    with pytest.raises(EvaluationCallError) as caught:
        fail()
    assert caught.value.logs==[{'node':'test_completed','duration_ms':23000}]
    assert turn_trace.get() is None


@pytest.mark.parametrize('mode,body,published,missing',[
 ('portable','車上有氧氣鋼瓶，具體供應請顧問核對。',True,True),
 ('portable','5000公尺以上景點提供隨身氧氣瓶，實際供應請顧問核對。',True,False),
 ('vehicle','車上有氧氣鋼瓶。',True,False),
 ('portable','請顧問核對。',False,False),
])
def test_semantic_supply_subject_cannot_be_satisfied_by_other_equipment(monkeypatch,mode,body,published,missing):
    from app.reception_v2.reply_scope import verify_scope
    audit={'event_error_checks':[],'question_checks':[],'contact_refusals':[],
        'requested_material_kinds':[],'consultant_task_checks':[],'unwanted_parts':[],
        'unwanted_part_checks':[],'oxygen_supply_request':{'mode':mode,'quote':'自己要準備嗎？'}}
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(deepcopy(audit),[],''))
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',lambda *args:([],[],''))
    result,_,_=verify_scope({'customer_message':'自己要準備嗎？','proposed_body':body,
        'request_reference_facts':[{'text':'5000公尺以上景點提供隨身氧氣瓶。'}] if published else []})
    assert bool(result.get('missing_answers')) is missing
