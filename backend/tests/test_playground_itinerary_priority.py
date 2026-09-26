"""Regress the business report: route photos overtook the itinerary in V1."""
import pytest

from app.config import settings
from app.route_packages import ROUTES, ROUTE_PACKAGES, JOURNEY_POLICY, _runtime_journey_sop
from app.delivery_plan import expand_static_delivery_nodes
from app.reception_v2.material_delivery import introduction_parts, introduction_sections


@pytest.mark.parametrize('route', ['peach_9d_2027', 'peach_11d_2027'])
def test_first_attachment_is_itinerary_in_both_engines(route):
    spec = ROUTES[route]
    itinerary = set(spec['groups']['itinerary_overview']['assets'])
    nodes = expand_static_delivery_nodes(_runtime_journey_sop(ROUTE_PACKAGES[route], JOURNEY_POLICY)['nodes'])
    v1_assets = [m['asset_key'] for node in nodes if node.get('initial_delivery')
                 for m in node.get('messages', []) if m.get('asset_key')]
    assert v1_assets and v1_assets[0] in itinerary
    assert set(v1_assets[:len(itinerary)]) == itinerary
    assert any(key not in itinerary for key in v1_assets)
    available = {key for group in spec['groups'].values() for key in group['assets']}
    sections = introduction_sections(spec, available, {'party_size': 4})
    parts = introduction_parts(sections, [{'asset_key': key} for key in available],
                               plan_id='business-order', interval_seconds=2)
    v2_assets = [p.material['asset_key'] for p in parts if p.kind == 'media']
    assert v2_assets and set(v2_assets[:len(itinerary)]) == itinerary
    assert len(v2_assets) == len(set(v2_assets))
    assert all(p.interval_seconds == 2 for p in parts[1:])


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_playground_api_defaults_follow_runtime_and_allow_explicit_comparison(monkeypatch, version):
    from app.automation_api import SessionCreate
    from app.evaluation_api import PlaygroundReplyRequest
    monkeypatch.setattr(settings, 'ai_engine_default', version)
    assert SessionCreate().engine_version == version
    assert PlaygroundReplyRequest(customer_message='你好').engine_version == version
    for explicit in ('v1', 'v2'):
        assert SessionCreate(engine_version=explicit).engine_version == explicit
        assert PlaygroundReplyRequest(customer_message='你好', engine_version=explicit).engine_version == explicit


def test_created_playground_session_uses_v2_release(authenticated, monkeypatch):
    from app.reception_v2 import ENGINE_RELEASE_ID
    monkeypatch.setattr(settings, 'ai_engine_default', 'v2')
    client, csrf = authenticated
    response = client.post('/v1/playground/sessions', headers={'X-CSRF-Token': csrf}, json={'mode': 'reply'})
    assert response.status_code == 201, response.text
    session = response.json()
    assert session['engine_version'] == 'v2'
    assert session['engine_release_id'] == ENGINE_RELEASE_ID
    assert session['outbound'] is False


@pytest.mark.parametrize('route', ['peach_9d_2027', 'peach_11d_2027'])
def test_v2_playground_delivered_timeline_keeps_itinerary_before_photos(session_factory, monkeypatch, route):
    from sqlalchemy import select
    import test_playground_journey as journey_fixture
    from app.automation_service import queue_passive, process_automation_run, confirm_draft
    from app.deepseek_evaluation import EvaluationDecision
    from app.models import OutboundMessage, utcnow

    monkeypatch.setattr(journey_fixture, 'ROUTE', route)
    spec = ROUTES[route]
    available = {key for group in spec['groups'].values() for key in group['assets']}
    sections = introduction_sections(spec, available, {'party_size': 4})
    model_decision = EvaluationDecision(
        action='reply', branch=spec['branch'], intent='itinerary', route_variant=route,
        reply=sections[0]['text'], content_group_key=sections[0]['group_key'],
        covered_content_groups=[s['group_key'] for s in sections],
        material_keys=[key for s in sections for key in s['asset_keys']],
        v2_delivery_sections=sections,
    )
    monkeypatch.setattr('app.automation_service.generate_decision', lambda _: (model_decision, [], 'hash', {}))
    def resolved(_db, keys, *_args, **_kwargs):
        return [{'asset_key': key, 'media_id': 100 + sorted(available).index(key),
                 'media_hash': key, 'content_family': key, 'content_type': 'image',
                 'route_variant': route} for key in keys]
    monkeypatch.setattr('app.automation_service.resolve_materials', resolved)
    monkeypatch.setattr('app.automation_service.material_info', lambda _db, item, *_args: item)
    monkeypatch.setattr('app.automation_service.previous_delivery', lambda *_args, **_kw: None)
    monkeypatch.setattr('app.automation_service.record_delivery', lambda *_args, **_kw: True)
    with session_factory() as db:
        session, _ = journey_fixture.setup_journey(db)
        session.engine_version = 'v2'
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment='playground')
        assert process_automation_run(db, environment='playground')
        db.refresh(session)
        drafts = [m for m in session.messages if m.get('status') == 'draft']
        assert drafts
        confirm_draft(db, session, drafts[-1]['id'])
        delivered = sorted([m for m in session.messages if m.get('status') == 'simulated_delivered'],
                           key=lambda m: m['timeline_sequence'])
        assets = [m['asset_key'] for m in delivered if m.get('asset_key')]
        itinerary = spec['groups']['itinerary_overview']['assets']
        assert assets[:len(itinerary)] == itinerary
        assert len(assets) == len(model_decision.material_keys)
        assert len(assets) == len(set(assets))
        assert db.scalar(select(OutboundMessage)) is None
