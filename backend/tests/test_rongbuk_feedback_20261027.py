from app.reception_v3.skills import service_knowledge
from app.reception_v3 import runtime
from test_reception_v3_skills import context, model, final


def test_rongbuk_scoped_fact_reaches_model_with_provenance(monkeypatch):
    knowledge = service_knowledge()
    ctx = context('peach_11d_2027')
    ctx['skills']['service_knowledge'] = knowledge
    requests = model(monkeypatch, [final(messages=[{'text': '需要按月份確認。'}])])
    runtime.run_agent(ctx)
    import json
    payload = json.dumps(requests[0], ensure_ascii=False)
    assert 'service.rongbuk_reservation_scope' in payload
    assert 'business.feedback.20261027' in payload
    assert '12–2月仍按现有冬季规则' in payload
    assert '不代表已替当前客户锁房' in payload
