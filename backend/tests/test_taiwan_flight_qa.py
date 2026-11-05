"""The flight knowledge reaches the model before generation, with provenance."""
import hashlib
from app.reception_v3 import skills, runtime
from app.route_packages import PACKAGE_ROOT
from test_reception_v3_skills import context, model, final, tool, task_payload


def test_flight_facts_reach_model_and_detail_tool(session_factory, monkeypatch):
    with session_factory() as db:
        c = context('peach_9d_2027')
        c['skills'] = skills.compile_skills(db)
        calls = model(monkeypatch, [
            {'tool_calls': [tool('get_service_facts', {})]},
            final(messages=[{'text': '台灣至成都可自行訂票，入藏段先核對指定航班。'}])])
        runtime.run_agent(c)
        facts = task_payload(calls[0])['service_knowledge']['facts']
        assert 'service.taiwan_flights.booking' in {f.get('id') for f in facts}
        result = next(m['content'] for m in calls[1]['messages'] if m['role'] == 'tool')
        assert '不把一般訂票網站難查到解釋為法律禁止自行購票' in result
        source = next(s for s in skills.service_knowledge()['sources']
                      if s['id'] == 'business.pdf.taiwan_flights.20261105')
        data = (PACKAGE_ROOT.parent/'service-knowledge'/source['snapshot_path']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == source['snapshot_sha256']
