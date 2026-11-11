"""Read-only rendering acceptance using the running backend and a DB reader.

No models or channel transports. The caller chooses original/candidate catalog.
"""
from copy import deepcopy
from types import SimpleNamespace


def verify(db):
    from app.reception_v3.skills import compile_skills
    from app.reception_v3.service import parts_for
    from app.reception_v3.runtime import Decision
    from app.reception_v3.seasonal_quotes import seasonal_group
    from app.automation_models import AutomationSession
    reference = db.get(AutomationSession, 972)  # #676: preserve real material tenant.
    assert reference is not None
    bundle = compile_skills(db)
    results = []
    for route in ('tibet_essence_8d', 'tibet_overview_10d'):
        spec = bundle['routes'][route]
        for month in ('', '2027年6月', '2027年2月', '12月或6月'):
            row = SimpleNamespace(id=0, generation=0, tenant_id=None,
                conversation_state_id=None, inbox_binding_id=reference.inbox_binding_id, environment='evaluation',
                controls={'route_variant': route}, messages=[],
                memory={'departure_window': month} if month else {})
            decision = Decision(route_variant=route, start_introduction=True).model_dump()
            _, parts = parts_for(db, row, bundle, decision)
            texts = [p['content'] for p in parts if p.get('content')]
            for group in ('vehicle_reference', 'vehicle_oxygen', 'rongbuk'):
                effective = seasonal_group(spec['groups'][group], row.memory)
                assert texts.count(effective['text']) == 1, (route, month, group)
            room_assets = set(spec['groups']['rongbuk']['assets'])
            actual_assets = {p.get('asset_key') for p in parts}
            if month == '2027年6月':
                assert room_assets <= actual_assets
                assert any('訂完就沒有' in t for t in texts)
            else:
                assert not room_assets & actual_assets
            if month == '2027年2月':
                assert not any('訂完就沒有' in t for t in texts)
            assert not any('移動氧艙' in t for t in texts)
            assert set(spec['groups']['vehicle_oxygen']['assets']) <= actual_assets
            # Answering an individual equipment question must not replay the intro.
            answer = Decision(route_variant=route, messages=[{'script_id': 'vehicle_oxygen'}]).model_dump()
            _, answer_parts = parts_for(db, row, bundle, answer)
            assert [p['content'] for p in answer_parts if p.get('content')] == [spec['groups']['vehicle_oxygen']['text']]
            results.append({'route': route, 'month': month or 'unknown',
                'parts': len(parts), 'texts': texts,
                'assets': sorted(k for k in actual_assets if k), 'single_question_passed': True})
    assert any('移動氧艙' in g['text'] for g in bundle['routes']['peach_11d_2027']['groups'].values())
    assert any('訂完就沒有' in g['text'] for g in bundle['routes']['peach_11d_2027']['groups'].values())
    return {'passed': True, 'scenarios': results, 'digest': bundle['digest']}
