from app.reception_v3 import consultation as c
from app.reception_v3.runtime import Decision
import pytest


@pytest.mark.parametrize('text', ['2027年【阿里環線15日】6人小團', '4–6人精品團', '2人成團'])
def test_capacity_cannot_be_customer_size(text):
    ctx={'messages':[{'id':'a','direction':'incoming','content':text}]}
    d=Decision(profile={'party_size':6},party_evidence={'kind':'actual','message_id':'a','quote':text}).model_dump()
    assert c.validation_error(d,ctx)


@pytest.mark.parametrize('text', ['我們6位', '4–6人小團，我們預計5人', '我們2大1小'])
def test_actual_party_with_or_without_ad(text):
    ctx={'messages':[{'id':'a','direction':'incoming','content':text}]}
    d=Decision(profile={'party_size':5},party_evidence={'kind':'actual','message_id':'a','quote':text}).model_dump()
    assert not c.validation_error(d,ctx)


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
