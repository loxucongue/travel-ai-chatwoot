import pytest


@pytest.mark.parametrize('mode,expected_calls,success',[
    ('connect_once',2,True),('connect_always',3,False),('read_always',2,False),
])
def test_connection_retry_is_bounded_and_does_not_repeat_stream_failures(monkeypatch,mode,expected_calls,success):
    import httpx
    import app.model_gateway as gateway
    from app.deepseek_evaluation import EvaluationCallError
    monkeypatch.setattr(gateway.settings,'deepseek_api_key','isolated-test')
    monkeypatch.setattr(gateway.time,'sleep',lambda _:None)
    calls=[]
    def post(payload,timeout):
        calls.append(timeout)
        if mode=='connect_always' or (mode=='connect_once' and len(calls)==1):
            raise httpx.ConnectError('isolated connection fault')
        if mode=='read_always':raise httpx.ReadTimeout('isolated stream fault')
        return httpx.Response(200,request=httpx.Request('POST','https://model.test'),
            json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
    monkeypatch.setattr(gateway,'_post_with_deadline',post)
    args=dict(node='test',system_prompt='test',input_data={},parser=lambda x:x,max_tokens=10,repair_prompt='test')
    if success:
        result,logs,_=gateway.call_json_node(**args)
        assert result=={'ok':True}
        assert sum(log['status']=='connection_retry' for log in logs)==1
        assert calls[1]<=calls[0]
    else:
        with pytest.raises(EvaluationCallError):gateway.call_json_node(**args)
    assert len(calls)==expected_calls


def test_connection_retry_cannot_start_after_turn_deadline(monkeypatch):
    import time
    import app.model_gateway as gateway
    from app.reception_v2.budget import deadline
    from app.deepseek_evaluation import EvaluationCallError
    monkeypatch.setattr(gateway.settings,'deepseek_api_key','isolated-test')
    monkeypatch.setattr(gateway,'_post_with_deadline',lambda *a:pytest.fail('Expired deadline'))
    token=deadline.set(time.monotonic()-1)
    try:
        with pytest.raises(EvaluationCallError,match='timeout'):
            gateway.call_json_node(node='test',system_prompt='test',input_data={},parser=lambda x:x,max_tokens=10,repair_prompt='test')
    finally:deadline.reset(token)
