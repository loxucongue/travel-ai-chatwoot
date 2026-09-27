"""Published service knowledge must survive V2 retrieval and final validation."""
import json
import pytest
from app.reception_v2.tools import execute_tool
from app.reception_v2.skill_registry import SkillRegistry

FACT={'id':'web.7.8.0.0','module_key':'official_contact','text':'客服微信為 approved_service。',
      'topics':['聯繫'],'source':'https://example.test/contact','branches':[]}


def test_playground_service_publication_does_not_enter_live_v2_tool(session_factory):
    from app.web_knowledge import install_curated_global_library, publish_revision, enrich_context_with_web_knowledge
    payload={'modules':[{'kind':'knowledge_module','key':'official_contact','title':'Contact',
        'summary':'Published contacts','topics':['contact'], 'facts':[
            {'text':'Approved contact details.','source_url':'https://example.test/contact','verified_at':'2026-09-21',
             'answer_requirements':[{'label':'Contact scope','any_of':['approved contact']}]}]}]}
    with session_factory() as db:
        source,revision=install_curated_global_library(db,payload,tenant_id=1,user_id=1)
        publish_revision(db,source,revision,runtime_scope='playground')
        db.commit()
        base={'customer_text':'contact'}
        live=enrich_context_with_web_knowledge(db,1,base,environment='live')
        playground=enrich_context_with_web_knowledge(db,1,base,environment='playground')
        with pytest.raises(ValueError,match='v2_service_module_unavailable'):
            execute_tool('get_service_facts',{'module_key':'official_contact'},SkillRegistry(),context=live)
        facts=execute_tool('get_service_facts',{'module_key':'official_contact'},SkillRegistry(),context=playground)['facts']
        assert facts[0]['answer_requirements']==payload['modules'][0]['facts'][0]['answer_requirements']


@pytest.mark.parametrize('requirements',[None,{},[{'label':'Scope','any_of':[]}],
    [{'label':'Scope','any_of':['']}],[{'label':'Scope','any_of':[42]}],
    [{'label':'Scope','any_of':['x'*161]}]])
def test_invalid_fact_conditions_rejected_before_publication(requirements):
    from app.web_knowledge import fact_answer_requirements, WebKnowledgeError
    with pytest.raises(WebKnowledgeError,match='website_fact_answer_requirements_invalid'):
        fact_answer_requirements({'answer_requirements':requirements})


def test_service_tool_only_reads_the_server_supplied_published_pool():
    context={'global_knowledge_candidates':[FACT,{'id':'untrusted','text':'wrong','module_key':'official_contact'}]}
    result=execute_tool('get_service_facts',{'module_key':'official_contact'},SkillRegistry(),context=context)
    assert result['facts']==[FACT]
    with pytest.raises(ValueError,match='v2_service_module_unavailable'):
        execute_tool('get_service_facts',{'module_key':'private_unpublished'},SkillRegistry(),context=context)
    with pytest.raises(ValueError,match='v2_service_module_unavailable'):
        execute_tool('get_service_facts',{'module_key':'official_contact'},SkillRegistry(),context={})


@pytest.mark.parametrize('prefetched',[False,True])
def test_service_fact_reaches_generator_auditor_and_outer_reference_validation(monkeypatch,prefetched):
    import app.reception_v2.runtime as runtime
    from app.reply_fact_verification import FactVerification
    from app.decision_service import generate_decision
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    calls=[]
    def call(payload,round_index):
        calls.append(payload)
        if len(calls)==1:
            assert payload['messages'][-1]=={'role':'user','content':'怎麼聯繫你們？'}
        if not prefetched and len(calls)==1:
            assert 'official_contact' in json.dumps(payload,ensure_ascii=False)
            return {'tool_calls':[{'id':'read_service','type':'function','function':{
                'name':'get_service_facts','arguments':json.dumps({'module_key':'official_contact'})}}]}, {'duration_ms':1}
        assert FACT['text'] in json.dumps(payload,ensure_ascii=False)
        return {'content':json.dumps({'action':'reply','route_variant':'peach_9d_2027',
            'reply':FACT['text'],'evidence_refs':[FACT['id']],
            'v2_events':[{'type':'question','quote':'怎麼聯繫你們？'}]})},{'duration_ms':1}
    def verify(context,decision):
        assert FACT in context['global_knowledge_facts']
        return FactVerification(True),[],''
    monkeypatch.setattr(runtime,'_call',call)
    decision,_,_,trace=generate_decision({'module':'reply','engine_version':'v2',
        'customer_text':'怎麼聯繫你們？','route_variant':'peach_9d_2027',
        'global_knowledge_version':'published-test',
        'global_knowledge_facts':[FACT] if prefetched else [],'global_knowledge_candidates':[FACT]})
    assert FACT['id'] in decision.evidence_refs
    assert 'unknown_evidence_reference_removed' not in decision.safety_flags
    assert trace['knowledge_usage']['used_fact_ids']==[FACT['id']]
