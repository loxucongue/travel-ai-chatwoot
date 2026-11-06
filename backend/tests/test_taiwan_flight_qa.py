"""The flight knowledge reaches the model before generation, with provenance."""
import hashlib
import json
from pathlib import Path
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
        assert 'service.taiwan_flights.flow' in {f.get('id') for f in facts}
        result = next(m['content'] for m in calls[1]['messages'] if m['role'] == 'tool')
        assert 'domestic_ranges' in result
        assert '3480' in result and '3880' in result
        assert '航空公司' in result
        source = next(s for s in skills.service_knowledge()['sources']
                      if s['id'] == 'website.taiwan-flight-sop.20261106')
        data = (PACKAGE_ROOT.parent/'service-knowledge'/source['snapshot_path']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == source['snapshot_sha256']


def test_ali_website_completion_only_extends_profile_label_configuration(session_factory, monkeypatch):
    from app.route_packages import _validate
    from app.reception_v3 import profile_labels
    from test_large_party_reception import setup
    releases = Path(__file__).resolve().parents[2]/'data/knowledge/china2go/route-releases'
    p = json.loads((releases/'20261106-ali-website-complete/route-package.json').read_text(encoding='utf8'))
    old = json.loads((releases/'20261105-ali-north-central/route-package.json').read_text(encoding='utf8'))
    _validate(p, releases/'20261106-ali-website-complete/route-package.json')
    for field in ('seasonal_pricing', 'content_groups', 'content_sequence', 'followup_policy'):
        assert p[field] == old[field]
    assert {k:v for k,v in p['large_party'].items() if k != 'completed_text'} == {k:v for k,v in old['large_party'].items() if k != 'completed_text'}
    with session_factory() as db:
        row = setup(db, monkeypatch, {'fitness_review': 'needed', 'group_type': 'private', 'inquiry_status': 'price_asked'})
        profile_labels.apply(db, row, p)
        profile_labels.apply(db, row, p)
        assert set(row.controls['evaluation_labels']) == {'體能需顧問評估', '高價值・整團', '已問價'}
        assert len(row.controls['evaluation_labels']) == 3
