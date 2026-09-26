import pytest

@pytest.mark.parametrize('body,accepted',[
 ('超過75歲不建議報名，個案請顧問核對。',True),
 ('76歲我們較建議不報名，個案請顧問核對。',True),
 ('建議先不要報名，個案請顧問核對。',True),
 ('建議報名參加，個案請顧問核對。',False),
 ('個案請顧問核對。',False),
])
def test_over75_equivalent_negative_recommendation_preserved(monkeypatch,body,accepted):
    from app.reception_v2.runtime import _verify
    from app.deepseek_evaluation import EvaluationDecision
    from app.reply_fact_verification import FactVerification
    audit=FactVerification(True,scope_check={'traveler_age_checks':[{'age':76,'taiwan_traveler':True}]})
    monkeypatch.setattr('app.reception_v2.runtime.call_reply_fact_verifier',lambda *a,**kw:(audit,[],''))
    decision=EvaluationDecision('handoff','peach_9d','other',reply=body,
        handoff_reason='knowledge_confirmation_required',evidence_refs=['service.peach_age'])
    result,_,_=_verify({'engine_version':'v2','customer_text':'台灣人76歲能參加嗎？'},decision)
    assert (not any('超过75岁' in v for v in result.contract_violations)) is accepted


@pytest.mark.parametrize('prefix,proven,repair',[
 ('小費建議每人每天30元，團費不包含小費；','團費不包含小費',True),
 ('小費建議每人每天30元，如果您還想確認；','小費建議每人每天30元',False),
 ('還有不確定的收費；','其他不在正文的事實',False),
])
def test_semicolon_suffix_prune_keeps_proved_answer(monkeypatch,prefix,proven,repair):
    from app.reception_v2.reply_revision import prune_copy
    from app.reply_fact_verification import FactVerification
    from app.deepseek_evaluation import EvaluationDecision
    tail='導遊與司機分別計費的細節請顧問確認。'
    ref='route.shared.tips'
    d=EvaluationDecision('reply','peach_9d','other',reply=prefix+tail,evidence_refs=[ref])
    a=FactVerification(True,claim_checks=[{'claim':proven,'supported':True,'evidence':ref}],
        scope_check={'unwanted_parts':[tail]})
    monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('No generation'))
    result=prune_copy({},d,a,{ref})
    if repair:
        assert result and result[0]['reply']==prefix.rstrip('；')+'。'
    else:
        assert result is None


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


@pytest.mark.parametrize('body,caption,expected',[
 ('波密與珠峰是例外；波密以外的其他地區安排希爾頓。','11日除波密與珠峰段外，其餘安排希爾頓。',['珠峰']),
 ('波密與珠峰是例外；這兩段以外安排希爾頓。','11日除波密與珠峰段外，其餘安排希爾頓。',[]),
 ('波密與珠峰段以外的其他地區安排希爾頓。','11日除波密與珠峰段外，其餘安排希爾頓。',[]),
 ('波密以外的其他地區安排希爾頓。','9日除波密外，其餘安排希爾頓。',[]),
 ('波密是例外，珠峰住絨布，不是波密以外所有地方都希爾頓。','11日除波密與珠峰段外，其餘安排希爾頓。',[]),
])
def test_every_explicit_hotel_exclusion_matches_route(body,caption,expected):
    from app.reception_v2.claim_guards import missing_hotel_exceptions
    assert missing_hotel_exceptions(body,caption)==expected


@pytest.mark.parametrize('kind',['repeat_content','unrequested_topic'])
@pytest.mark.parametrize('quote,protected',[
 ('波密和珠峰是希爾頓升級例外',True),
 ('珠峰住絨布旅館有供氧與獨立衛浴',False),
])
def test_proactive_required_exception_is_not_unrequested(kind,quote,protected):
    from app.reception_v2.reply_scope import preserve_required_hotel_exceptions
    body='全程供氧住宿，波密和珠峰是希爾頓升級例外，其餘地區安排希爾頓飯店。珠峰住絨布旅館有供氧與獨立衛浴。'
    # To test necessity, do not add another Everest mention for the protected case.
    if protected:body=body.split('。')[0]+'。'
    result={'unwanted_parts':[quote],'unwanted_part_checks':[{'quote':quote,'kind':kind}]}
    preserve_required_hotel_exceptions({'event':'silence_due','selected_route':'peach_11d_2027','proposed_body':body},result)
    assert (quote not in result['unwanted_parts']) is protected
