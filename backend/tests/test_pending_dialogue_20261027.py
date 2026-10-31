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
