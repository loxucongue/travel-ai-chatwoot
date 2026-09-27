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
