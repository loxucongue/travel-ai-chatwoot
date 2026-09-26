import json
from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.deepseek_evaluation import EvaluationDecision
from app.reply_fact_verification import FactVerification

@pytest.mark.parametrize('body,blocked',[
 ('臺灣65至75歲需要健康證明，64歲不在此列。',True),
 ('健康證明適用65至75歲，您64歲不受這項年齡段要求限制。',True),
 ('65至75歲需健康證明，不能據此認定64歲不在此列。',False),
 ('您的健康證明要求需要按實際證件核對。',False),
])
def test_certificate_anaphora(body,blocked):
 from app.reception_v2.claim_guards import unsupported_certificate_waiver
 assert unsupported_certificate_waiver(body)==blocked

@pytest.mark.parametrize('body,blocked',[
 ('供氧是輔助設備，能讓旅途舒服一些。',True),
 ('供氧是讓住宿和車上舒適一些，不代表不會高反。',True),
 ('車上有供氧設備，不保證能讓旅途舒服一些。',False),
 ('供氧不能讓每個人都舒服，個人狀況需醫師评估。',False),
 ('房間有供氧和獨立衛浴。',False),
])
def test_equipment_not_effect(body,blocked):
 from app.reception_v2.claim_guards import unsupported_oxygen_effect
 assert unsupported_oxygen_effect(body)==blocked

@pytest.mark.parametrize('body,claim,expected',[
 ('入藏函在成都交付。若您不是從成都進藏，交付方式需由顧問核對。','交付方式需由顧問核對','入藏函在成都交付。'),
 ('入藏函在成都交付，行前顧問會跟您約好時間地點。','行前顧問會跟您約好時間地點','入藏函在成都交付。'),
])
def test_clause_removal_keeps_complete_supported_answer(monkeypatch,body,claim,expected):
 from app.reception_v2.reply_revision import revise_copy
 d=EvaluationDecision('reply','peach_9d','other',reply=body)
 a=FactVerification(False,unsupported_claims=[claim],claim_checks=[{'claim':'入藏函在成都交付','supported':True,'evidence':'permit'}])
 monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('No model needed for complete remainder'))
 assert revise_copy({},d,a,set())[0]['reply']==expected

@pytest.mark.parametrize('compound',[False,True])
def test_profile_receipt_runs_even_after_accepted_audit(compound):
 from app.reception_v2.state_revision import revise_grounded_state
 d=EvaluationDecision('reply','peach_9d','other',reply='4位記下了，行程圖接著傳給您。',slots={'party_size':'4'},
     material_keys=['old-image'],v2_events=[{'type':'profile_updated','quote':'我們4位'}])
 scope={'profile_ack_only':True,'profile_update_checks':[{'field':'party_size','quote':'我們4位','value':'4','persisted_correctly':True}]}
 if compound:scope['question_checks']=[{'request_kind':'fact','request_quote':'小費多少'}]
 a=FactVerification(True,scope_check=scope)
 result=revise_grounded_state({'customer_text':'我們4位'}, {'reply':d.reply},d,a)
 if compound:assert result is None
 else:assert result['reply']=='好喔，同行4位。' and result['material_keys']==[]

@pytest.mark.parametrize('stage',['appointment','contact_agreed','follow_up_scheduled'])
def test_validated_appointment_owns_stage(stage):
 from app.reception_v2.runtime import _validated_decision
 raw={'action':'reply','reply':'收到您的時間。','journey_stage':stage,'v2_events':[{'type':'contact_agreed','quote':'後天早上10點再聯繫我','contact_at':'2026-09-22T10:00:00+08:00'}]}
 d=_validated_decision({'content':json.dumps(raw)},set(),set(),{'customer_text':'後天早上10點再聯繫我','now':'2026-09-20T02:00:00+00:00'})
 assert d.journey_stage=='considering'

@pytest.mark.parametrize('nationality,valid',[('台灣人',True),('approved fact says Taiwan',False),('',False)])
def test_identity_proof_is_inside_schema_repair_parser(monkeypatch,nationality,valid):
 from app.reception_v2.reply_scope import verify_scope
 value=dict(current_request='年齡',required_answer='年齡',question_checks=[],event_error_checks=[],material_request_checks=[],profile_update_checks=[],
   traveler_age_checks=[{'age':64,'quote':'64歲','taiwan_traveler':True,'nationality_quote':nationality}],contact_refusals=[],unwanted_part_checks=[],requested_material_kinds=[],consultant_task_checks=[])
 def call(**kw):
  if not valid:
   with pytest.raises(ValueError,match='nationality'):kw['parser'](deepcopy(value))
   value['traveler_age_checks'][0]['taiwan_traveler']=False
  return kw['parser'](deepcopy(value)),[],''
 monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',call)
 result=verify_scope({'customer_message':'台灣人64歲','proposed_body':'64歲不是最低年齡。'})[0]
 assert result['traveler_age_checks'][0]['taiwan_traveler']==valid


@pytest.mark.parametrize('body,blocked',[
 ('馬來西亞的健康證明要求跟台灣不完全一樣，需要顧問核對。',True),
 ('健康證明的規定會依證件不同，需核對。',True),
 ('不能認定馬來西亞的健康證明要求跟台灣不完全一樣。',False),
 ('馬來西亞的健康證明要求要按實際證件核對。',False),
])
def test_unknown_identity_rules_do_not_imply_difference(body,blocked):
 from app.reception_v2.claim_guards import unsupported_certificate_difference
 assert unsupported_certificate_difference(body)==blocked


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


def test_profile_ack_repair_is_idempotent():
 from app.reception_v2.runtime import _validated_decision
 from app.reception_v2.state_revision import revise_grounded_state
 context={'module':'reply','customer_text':'我們4位','route_variant':'peach_9d_2027'}
 raw={'action':'reply','reply':'同行4位，接著傳圖。','slots':{'party_size':'4'},'slot_evidence':{'party_size':'我們4位'},'v2_events':[{'type':'profile_updated','quote':'我們4位'}]}
 d=_validated_decision({'content':json.dumps(raw)},set(),set(),context)
 a=FactVerification(True,scope_check={'profile_ack_only':True,'profile_update_checks':[{'field':'party_size','persisted_correctly':True}]})
 repaired=revise_grounded_state(context,raw,d,a)
 assert repaired
 d=_validated_decision({'content':json.dumps(repaired)},set(),set(),context)
 assert revise_grounded_state(context,repaired,d,a) is None


@pytest.mark.parametrize('quote,module,expected',[
 ('請不要再發推銷訊息給我','reply',[{'scope':'all'}]),
 ('historical only','reply',[]),
 ('請不要再發推銷訊息給我','silence_touch',[]),
])
def test_refusal_execution_evidence_reaches_fact_auditor(monkeypatch,quote,module,expected):
 import app.reply_fact_verification as proof
 from app.reception_v2.runtime import _verify
 proof._VERIFIER_CACHE.clear()
 captured=[]
 def model(**kw):
  captured.append(kw['input_data']['planned_system_action'].get('contact_refusals',[]))
  return FactVerification(True),[],''
 monkeypatch.setattr(proof,'call_json_node',model)
 monkeypatch.setattr('app.reception_v2.reply_scope.verify_scope',lambda data:({'unwanted_parts':[],'event_errors':[],'missing_answers':[],'consultant_tasks':[]},[],''))
 d=EvaluationDecision('reply','peach_9d','other',reply='之後不會再主動聯繫您。',route_variant='peach_9d_2027',
    v2_events=[{'type':'contact_refused','scope':'all','quote':quote}])
 _verify({'engine_version':'v2','module':module,'customer_text':'請不要再發推銷訊息給我'},d)
 assert captured==[expected]


@pytest.mark.parametrize('kind,has_caption,removed',[
 ('itinerary',True,True),('hotel',True,False),('itinerary',False,False),
])
def test_actual_requested_caption_is_not_an_unrequested_topic(monkeypatch,kind,has_caption,removed):
 from app.reception_v2.reply_scope import verify_scope
 part='從林芝低海拔入藏'
 value=dict(current_request='給圖',required_answer='圖片',question_checks=[],event_error_checks=[],
  material_request_checks=[{'kind':kind,'quote':'給圖'}],profile_update_checks=[],traveler_age_checks=[],contact_refusals=[],
  unwanted_part_checks=[{'quote':part,'kind':'unrequested_topic','reason':'只問圖片'}],requested_material_kinds=[kind],consultant_task_checks=[],unwanted_parts=[part])
 monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(deepcopy(value),[],''))
 data={'customer_message':'給圖','proposed_body':part+'。','v2_events':[{'type':'material_requested','material_kind':kind}],
  'compiled_material_captions':[{'kind':'itinerary','text':part+'。'}] if has_caption else []}
 assert (not verify_scope(data)[0]['unwanted_parts'])==removed


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


def test_fixed_optout_receipt_has_execution_proof_despite_model_false_negative(monkeypatch):
 import app.reply_fact_verification as proof
 from app.reception_v2.runtime import _verify
 from app.customer_contact_policy import V2_OPT_OUT_RECEIPT
 proof._VERIFIER_CACHE.clear()
 def model(**kw):
  return kw['parser']({'supported':False,'unsupported_claims':['之後不會再主動聯繫您'],'relevant':True,'unanswered_questions':[],
    'claim_checks':[{'claim':'之後不會再主動聯繫您','supported':False,'evidence':'missing','reason':'Model overlooked state'}]}),[],''
 monkeypatch.setattr(proof,'call_json_node',model)
 monkeypatch.setattr('app.reception_v2.reply_scope.verify_scope',lambda data:({'unwanted_parts':[],'event_errors':[],'missing_answers':[],'consultant_tasks':[]},[],''))
 d=EvaluationDecision('reply','peach_9d','other',reply=V2_OPT_OUT_RECEIPT,v2_events=[{'type':'contact_refused','scope':'all','quote':'不要聯繫我'}])
 result,_,_=_verify({'engine_version':'v2','module':'reply','customer_text':'不要聯繫我'},d)
 assert result.supported and not result.unsupported_claims


def test_internal_quote_explanation_is_not_customer_copy():
 from app.advisor_voice import v2_internal_copy_violation
 assert v2_internal_copy_violation('我這邊先不隨意報價喔。')
 assert not v2_internal_copy_violation('包團實際價格需要顧問核對。')



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



def test_answer_and_unasked_tail_in_same_segment_keep_direct_answer(monkeypatch):
 from app.reception_v2.reply_revision import revise_copy
 d=EvaluationDecision('reply','peach_9d','other',reply='多人同行有優惠，具體金額由顧問核對。')
 a=FactVerification(True,scope_check={'question_checks':[{'request_kind':'fact','covered':True,'answer_segment_ids':['reply_0']}],
    'unwanted_parts':['具體金額由顧問核對。']},claim_checks=[{'claim':'多人同行有優惠','supported':True,'evidence':'offer'}])
 monkeypatch.setattr('app.reception_v2.reply_revision.call_json_node',lambda **kw:pytest.fail('Do not regenerate the surviving direct answer'))
 assert revise_copy({},d,a,set())[0]['reply']=='多人同行有優惠。'
