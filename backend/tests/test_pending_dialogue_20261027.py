import pytest

def test_json_repair_includes_full_protocol_and_cannot_loop(monkeypatch):
    from app.reception_v3 import runtime
    from app.model_transport import EvaluationCallError
    from test_reception_v3_skills import context, model, final
    requests=model(monkeypatch,[{'content':'這是給客人的文字，不是JSON'}, final(messages=[{'text':'天氣要看月份。'}])])
    decision,logs,_=runtime.run_agent(context('peach_9d_2027'))
    assert decision['messages'][0]['text']=='天氣要看月份。'
    assert logs[0]['status']=='invalid_json'
    assert any('"properties"' in m.get('content','') for m in requests[1]['messages'])
    requests=model(monkeypatch,[{'content':''},{'content':''}])
    with pytest.raises(EvaluationCallError):runtime.run_agent(context('peach_9d_2027'))
    assert len(requests)==2


@pytest.mark.parametrize('text,expected',[
    ('好，我們看林芝進的全覽10天行程，六月出發。',True),
    ('我們想想，還沒決定要不要換10天',False),
    ('不想換，先看原本的',False),
])
def test_explicit_route_acceptance_after_date_discussion(text,expected):
    from app.reception_v3.route_choice import explicit_selection
    assert explicit_selection(text,'tibet_overview_10d',{'skills':{'routes':{}},'profile':{}})==expected
