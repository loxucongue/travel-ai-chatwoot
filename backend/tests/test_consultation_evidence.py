from app.reception_v3 import consultation as c
from app.reception_v3.runtime import Decision
import pytest


@pytest.mark.parametrize('text', ['2027年【阿里環線15日】6人小團', '4–6人精品團', '2人成團'])
def test_capacity_cannot_be_customer_size(text):
    ctx={'messages':[{'id':'a','direction':'incoming','content':text}]}
    d=Decision(profile={'party_size':6},party_evidence={'kind':'actual','message_id':'a','quote':text}).model_dump()
    assert c.validation_error(d,ctx)


@pytest.mark.parametrize('text,count', [('我們6位',6), ('4–6人小團，我們預計5人',5), ('我們2大1小',3)])
def test_actual_party_with_or_without_ad(text,count):
    ctx={'messages':[{'id':'a','direction':'incoming','content':text}]}
    d=Decision(profile={'party_size':count},party_evidence={'kind':'actual','message_id':'a','quote':text}).model_dump()
    assert not c.validation_error(d,ctx)
    d['profile']['party_size']=count+1
    assert c.validation_error(d,ctx)


def test_hypothetical_and_fabricated_source_rejected():
    d=Decision(profile={'party_size':6},party_evidence={'kind':'hypothetical','message_id':'a','quote':'如果6人'}).model_dump()
    assert c.validation_error(d,{'messages':[{'id':'a','direction':'incoming','content':'如果6人'}]})
    d['party_evidence']['kind']='actual'
    assert c.validation_error(d,{'messages':[{'id':'a','direction':'outgoing','content':'如果6人','status':'sent'}]})


def test_delivered_questions_and_undecided_are_remembered():
    ctx={'profile':{'_consultation_needs':{'departure':{'status':'undecided'}}},'messages':[
        {'id':'sent','direction':'outgoing','status':'sent','question_topic':'party'},
        {'id':'draft','direction':'outgoing','status':'draft','question_topic':'concern'}]}
    t=c.task(ctx)
    assert 'departure' not in t['missing_topics']
    assert t['asked']=={'party':'sent'}


def test_human_question_evidence_requires_delivery():
    d=Decision(asked_evidence=[{'topic':'departure','message_id':'h'}]).model_dump()
    ctx={'messages':[{'id':'h','direction':'outgoing','attribution':'human','status':'failed'}]}
    assert c.validation_error(d,ctx)
    ctx['messages'][0]['status']='sent'
    assert not c.validation_error(d,ctx)
    assert c.memory_updates(d,ctx['messages'],{})['_consultation_asked']=={'departure':'h'}


def test_need_must_have_customer_source_and_one_question():
    d=Decision(need_evidence=[{'topic':'departure','status':'undecided','message_id':'x','quote':'未定'}]).model_dump()
    assert c.validation_error(d,{'messages':[]})
    d=Decision(messages=[{'text':'幾位','question_topic':'party'},{'text':'幾月','question_topic':'departure'}]).model_dump()
    assert c.validation_error(d,{})


def test_capacity_cannot_be_personalized_in_reply_even_without_profile_write():
    ctx={'messages':[{'id':'ad','direction':'incoming','content':'阿里15天2人成團'}]}
    d=Decision(messages=[{'text':'所以2位还不能单独成团'}]).model_dump()
    assert c.validation_error(d,ctx)
    d['messages'][0]['text']='这条产品按已公布成团规则安排，请问您实际几位同行？'
    assert not c.validation_error(d,ctx)


def test_latest_explicit_need_replaces_undecided_memory():
    memory={'_consultation_needs':{'departure':{'status':'undecided','message_id':'old'}}}
    d=Decision(need_evidence=[{'topic':'departure','status':'known','message_id':'new','quote':'2027年3月底'}]).model_dump()
    messages=[{'id':'new','direction':'incoming','content':'2027年3月底'}]
    assert not c.validation_error(d,{'messages':messages})
    assert c.memory_updates(d,messages,memory)['_consultation_needs']['departure']['status']=='known'


def test_legacy_capacity_only_profile_must_be_rechecked_without_bulk_reset():
    ctx={'profile':{'party_size':6,'group_type':'private'},
         'messages':[{'id':'ad','direction':'incoming','content':'阿里15日6人小團'}]}
    d=Decision(start_introduction=True).model_dump()
    assert c.validation_error(d,ctx)
    d['profile']={'party_size':None,'party_size_min':None,'party_size_range':None,'group_type':'undecided'}
    assert not c.validation_error(d,ctx)
    assert ctx['profile']['party_size']==6
