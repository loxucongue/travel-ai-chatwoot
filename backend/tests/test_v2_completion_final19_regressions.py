import json
from copy import deepcopy
import pytest
from app.deepseek_evaluation import EvaluationDecision


@pytest.mark.parametrize('stage',['appointment','contact_agreed','follow_up_scheduled'])
def test_validated_appointment_owns_stage(stage):
 from app.reception_v2.runtime import _validated_decision
 raw={'action':'reply','reply':'收到您的時間。','journey_stage':stage,'v2_events':[{'type':'contact_agreed','quote':'後天早上10點再聯繫我','contact_at':'2026-09-22T10:00:00+08:00'}]}
 d=_validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'後天早上10點再聯繫我','now':'2026-09-20T02:00:00+00:00'})
 assert d.journey_stage=='considering'


def test_reasoning_timeout_fallback_keeps_original_deadline_and_parser(monkeypatch):
 import time,httpx
 import app.model_gateway as gateway
 from app.reception_v2.budget import deadline
 monkeypatch.setattr(gateway.settings,'deepseek_api_key','test-key')
 calls=[]
 def post(payload,timeout):
  calls.append((deepcopy(payload),timeout))
  if len(calls)==1:raise TimeoutError('reasoning time slice')
  return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]},request=httpx.Request('POST','https://example.test'))
 monkeypatch.setattr(gateway,'_post_with_deadline',post)
 token=deadline.set(time.monotonic()+12)
 try:
  original=deadline.get()
  result,logs,digest=gateway.call_json_node(node='test',system_prompt='test',input_data={'evidence':'same'},parser=lambda x:x,max_tokens=3000,
    repair_prompt='test',reasoning_effort='low',reasoning_fallback_tokens=1800,reasoning_timeout_seconds=6)
  assert result=={'ok':True} and len(logs)==2
  assert calls[0][1]<=6 and calls[0][0]['messages']==calls[1][0]['messages']
  assert calls[1][0]['thinking']['type']=='disabled' and deadline.get()==original
 finally:deadline.reset(token)


def test_optout_receipt_cannot_hide_a_separate_question():
 from app.reception_v2.runtime import _enforce_delivery_contract
 from app.customer_contact_policy import V2_OPT_OUT_RECEIPT
 d=EvaluationDecision('reply','peach_9d','other',reply='在林芝集合。',v2_events=[
  {'type':'contact_refused','scope':'all','quote':'不要聯繫我'}, {'type':'question','quote':'在哪集合'}])
 _enforce_delivery_contract({'customer_text':'不要聯繫我，在哪集合'},d)
 assert d.reply=='在林芝集合。'
 d.v2_events=d.v2_events[:1]
 _enforce_delivery_contract({'customer_text':'不要聯繫我'},d)
 assert d.reply==V2_OPT_OUT_RECEIPT


def test_certificate_configuration_is_shared_without_disabling_verification(monkeypatch):
 import ssl
 from concurrent.futures import ThreadPoolExecutor
 import app.deepseek_evaluation as transport
 monkeypatch.setattr(transport,'_model_tls',None)
 ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
 calls=[]
 def create(**kw):
  calls.append(kw)
  return ctx
 monkeypatch.setattr(transport.httpx,'create_ssl_context',create)
 with ThreadPoolExecutor(max_workers=4) as pool:
  contexts=list(pool.map(lambda _:transport._model_ssl_context(),range(8)))
 assert all(item is ctx for item in contexts)
 assert calls==[{'verify':True,'trust_env':True}]
 assert ctx.check_hostname and ctx.verify_mode==ssl.CERT_REQUIRED
