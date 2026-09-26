"""Pre-registered service-knowledge and elliptical-question model acceptance."""
import hashlib
import json
import os
from pathlib import Path
import re
import httpx
import pytest
from app.config import settings
from app.decision_service import generate_decision
from app.release_provenance import source_fingerprint, assert_source_unchanged

CASES=[
    ('company','你們公司在哪裡？官網網址也給我。','peach_9d_2027',[],['上海','https://china2go.com']),
    ('line','你們官方LINE帳號是多少？','peach_9d_2027',[],['@315xvpvi']),
    ('oxygen_ellipsis','自己要準備嗎？','peach_11d_2027',[
        {'role':'customer','content':'桃花11日下車走行程也有氧氣嗎？'},
        {'role':'assistant','content':'我幫您看下車活動時的供氧安排。'}],['5000|5,000|五千','隨身|随身','核對|確認|確認|顧問']),
    ('vegetarian','我是素食者，這趟吃飯能配合嗎？','peach_9d_2027',[],['素食','顧問|確認|核對']),
    ('flight','可以幫忙訂機票嗎？機票算在9980裡嗎？','peach_9d_2027',[],['代訂|訂票|訂機票','不含|不包含|未含|未包含|不在(?:團費|包含項目|包含範圍)(?:內|裡)']),
    ('vehicle_priority','網站說車型要確認，那四到六人桃花團用什麼車？','peach_9d_2027',[],['2025','VIP|航空']),
    ('hotel_priority','網站說偏遠住宿簡樸，11日珠峰那晚有獨立衛浴嗎？','peach_11d_2027',[],['獨立','絨布']),
    ('oxygen_scope','9日每天在戶外都有每人一支氧氣瓶嗎？','peach_9d_2027',[],['5000|5,000|五千','顧問|核對|確認']),
]


@pytest.mark.skipif(os.environ.get('VERIFY_V2_JOURNEYS')!='1',reason='Explicit model-only paid acceptance')
@pytest.mark.parametrize('repeat',range(int(os.environ.get('V2_SERVICE_REPEATS','3'))))
@pytest.mark.parametrize('name,customer,route,history,required',CASES,ids=[c[0] for c in CASES])
def test_published_service_questions(monkeypatch,name,customer,route,history,required,repeat):
    from app.reply_fact_verification import _VERIFIER_CACHE
    _VERIFIER_CACHE.clear()
    original, original_async=httpx.Client.send,httpx.AsyncClient.send
    endpoint=settings.deepseek_base_url.rstrip('/')+'/chat/completions'
    def send(client,request,*args,**kwargs):
        assert str(request.url)==endpoint and request.method=='POST'
        return original(client,request,*args,**kwargs)
    async def send_async(client,request,*args,**kwargs):
        assert str(request.url)==endpoint and request.method=='POST'
        return await original_async(client,request,*args,**kwargs)
    monkeypatch.setattr(httpx.Client,'send',send)
    monkeypatch.setattr(httpx.AsyncClient,'send',send_async)
    source=Path('../data/knowledge/china2go/global-website-knowledge/curated-modules.json')
    payload=json.loads(source.read_text(encoding='utf8'))
    facts=[{'id':f'web.700.1.{i}.{j}','module_key':module['key'],'topics':module['topics'],
        'text':fact['text'],'source':fact['source_url'],'branches':[],
        'answer_requirements':fact.get('answer_requirements',[])}
        for i,module in enumerate(payload['modules']) for j,fact in enumerate(module['facts'])]
    fingerprint=source_fingerprint()
    report={'id':name,'repeat':repeat+1,'input':customer,'history':history,'route':route,
        'source_fingerprint':fingerprint,'service_fixture_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'knowledge_scope':'isolated approved fixture; does not approve live sources','real_customer_messages':0,'pass':False}
    try:
        decision,calls,_,trace=generate_decision({'module':'reply','engine_version':'v2','customer_text':customer,
            'context_messages':history,'route_variant':route,'source_message_id':'service-test',
            'now':'2026-09-21T02:00:00+00:00','global_knowledge_version':'isolated-reviewed-fixture',
            'global_knowledge_facts':[],'global_knowledge_candidates':facts})
        from dataclasses import asdict
        report.update(decision=asdict(decision),calls=calls,trace=trace)
        assert decision.reply
        assert all(re.search(pattern,decision.reply) for pattern in required),decision.reply
        assert '比較' not in decision.reply and '比较' not in decision.reply
        if name in {'company','line','flight','vehicle_priority','hotel_priority'}:
            assert decision.action=='reply',decision
        if name in {'company','line','oxygen_ellipsis','vegetarian','flight','oxygen_scope'}:
            assert trace['knowledge_usage']['used_fact_ids'], 'published service evidence not used'
        assert_source_unchanged(fingerprint)
        report['pass']=True
    except Exception as exc:
        report['error']=str(exc)[:500]
        report['failure_logs']=getattr(exc,'logs',[])
        raise
    finally:
        folder=Path(os.environ.get('V2_SERVICE_OUTPUT','../output/v2-completion-20260920/service-acceptance'))
        folder.mkdir(parents=True,exist_ok=True)
        (folder/f'{name}-{repeat+1}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
