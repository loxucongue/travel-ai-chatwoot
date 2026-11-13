"""Exercise real installation, config reload, asset resolution and reply rendering."""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output/beijing-acceptance'
STAGE = OUT / 'stage'
RELEASE = ROOT / 'data/knowledge/china2go/route-releases/20261113-beijing'


def main():
    database = OUT / f'integration-{time.time_ns()}.db'
    if database.exists():
        raise ValueError('use_new_acceptance_database')
    os.environ.update(APP_PROFILE='evaluation', DATABASE_URL='sqlite:///' + database.as_posix(),
                      OUTBOUND_MODE='disabled', CHATWOOT_WRITE_ENABLED='false', LIVE_SOP_ENABLED='false',
                      RELAY_BASE_URL='', RELAY_API_TOKEN='', CUSTOMER_IMAGE_VISION_ENABLED='false')
    os.chdir(STAGE / 'backend')
    sys.path.insert(0, str(STAGE / 'backend'))
    from app.main import app
    from app.db import Base, engine
    from sqlalchemy import select, func
    from sqlalchemy.orm import Session
    from app.models import Tenant, User, KnowledgeVersion, MaterialAsset, StoredMedia, InboxBinding
    from app.automation_models import AutomationSession
    from app.reception_config import ReceptionConfiguration, put_reception_configuration, get_reception_configuration
    from app.reception_v3.skills import compile_skills, SkillRegistry
    from app.material_library import candidate_materials, resolve_materials
    from app.reception_v3 import service
    from app.reception_v3.runtime import Decision
    from install_beijing_package import install
    baseline = json.loads((OUT / 'baseline.json').read_text(encoding='utf8'))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name='Beijing isolated acceptance'))
        db.flush()
        db.add(User(id=1, email='beijing@example.invalid', display_name='Acceptance', password_hash='unused', role='super_admin'))
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=1, name='isolated', ai_enabled=False))
        db.flush()
        db.add(KnowledgeVersion(id=4, tenant_id=1, version_key='china2go-peach-two-products-20260829-v1', title='acceptance', content_hash='beijing', source_summary={}))
        db.flush()
        put_reception_configuration(db, ReceptionConfiguration.model_validate(baseline['config']))
        before = get_reception_configuration(db)
        result = install(db, RELEASE, tenant_id=1, user_id=1, activate=True)
        db.commit()
        install(db, RELEASE, tenant_id=1, user_id=1, activate=True)
        db.commit()
        assert db.scalar(select(func.count()).select_from(MaterialAsset)) == 40
        assert db.scalar(select(func.count()).select_from(StoredMedia)) == 40
        after = get_reception_configuration(db)
        assert after['routing']['enabled_route_variants'] == before['routing']['enabled_route_variants'] + ['beijing_micro_5d']
        for key in before:
            if key not in ('routing', 'material_rules'):
                assert before[key] == after[key], key
        for key in before['routing']:
            if key != 'enabled_route_variants':
                assert before['routing'][key] == after['routing'][key], key
        assert after['material_rules'][:len(before['material_rules'])] == before['material_rules']
        bundle = compile_skills(db)
        materials = candidate_materials(db, 1)
        assert len(materials) == 40
        manifest = json.loads((RELEASE / 'asset-manifest.json').read_text(encoding='utf8'))
        for item in manifest:
            assert resolve_materials(db, [item['asset_key']], 'beijing_micro_5d', 1)
        assert SkillRegistry(bundle, materials).route_skill('beijing_micro_5d') == 'beijing-micro-5d'
        row = AutomationSession(owner_id=1, inbox_binding_id=1, engine_version='beijing-test', mode='journey', environment='playground',
                                messages=[], memory={}, controls={'route_variant': 'beijing_micro_5d'}, virtual_now='2026-11-13T04:00:00+00:00')
        db.add(row)
        db.flush()
        intake_state = {}
        assert service.begin_intake(row, intake_state, bundle, 'beijing_micro_5d')
        assert intake_state['intake_wait_seconds'] == 30
        row.messages = []
        row.memory = {'party_size': 2}
        assert not service.begin_intake(row, {}, bundle, 'beijing_micro_5d')
        intro = Decision(route_variant='beijing_micro_5d', start_introduction=True).model_dump()
        _, intro_parts = service.parts_for(db, row, bundle, intro)
        assert len([p for p in intro_parts if p['content_type'] == 'image']) == 9
        assert len([p for p in intro_parts if p['content_type'] == 'text']) == 9
        from app.reception_v3.material_rules import itinerary_previews
        assert itinerary_previews(bundle, materials, [])['beijing_micro_5d']['assets'][0]['key'] == 'beijing_20261113_01'
        from app.reception_v3.route_followup import apply as checkpoint
        quote_decision = Decision(followup_checkpoint='quote_wait').model_dump()
        assert checkpoint({}, quote_decision, bundle, 'beijing_micro_5d', {}) == 180
        for permission in ('opt_out', 'handoff', 'lead_assist'):
            assert checkpoint({permission: True}, quote_decision, bundle, 'beijing_micro_5d', {}) is None
        from app.reception_v3.profile_labels import apply as labels
        row.memory = {'reception_status': 'sensitive'}
        labels(db, row, bundle['routes']['beijing_micro_5d'])
        assert '敏感・轉真人' in row.controls['evaluation_labels']
        for kind, label in [('urgent', '急件'), ('custom', '北京訂製'), ('large', '高價值・北京包團')]:
            row.memory = {'beijing_request_kind': kind}
            labels(db, row, bundle['routes']['beijing_micro_5d'])
            assert label in row.controls['evaluation_labels'], kind
        row.memory = {'party_size': 2}
        followup_checks = []
        for n in range(1, 6):
            decision = Decision(route_variant='beijing_micro_5d', messages=[{'material_rule_id': f'beijing_daily_{n}'}]).model_dump()
            _, parts = service.parts_for(db, row, bundle, decision, followup=True)
            assert len([p for p in parts if p['content_type'] == 'image']) <= 1
            assert any(p['content_type'] == 'text' for p in parts)
            followup_checks.append(n)
        checks = []
        latest = {}
        for name in ('model-results.json', 'boundary.json', 'revision2.json', 'final-revision.json', 'child-final.json', 'release-model.json', 'multiround.json', 'extended.json', 'extended-revision.json', 'precision-final.json', 'optimized-final.json', 'switch-final.json'):
            if (OUT / name).exists():
                latest.update({c['id']: c for c in json.loads((OUT / name).read_text(encoding='utf8'))})
        for case in latest.values():
            if 'decision' not in case:
                checks.append({'case': case['id'], 'error': 'no_model_decision'})
                continue
            try:
                row.controls = {**row.controls, 'route_variant': case.get('initial_route', 'beijing_micro_5d')}
                decision = Decision.model_validate(case['decision']).model_dump()
                route, parts = service.parts_for(db, row, bundle, decision)
                checks.append({'case': case['id'], 'parts': parts})
                if case.get('expected_profile'):
                    probe = AutomationSession(owner_id=1, inbox_binding_id=1, environment='playground',
                        memory=decision['profile'], controls={'route_variant': ''}, messages=[])
                    policy = bundle['routes'].get(decision['route_variant'] or probe.controls['route_variant'], {})
                    labels(db, probe, policy, routes=bundle['routes'], updates=decision['profile'])
                    expected_label = '急件' if case['expected_profile'].get('beijing_request_kind') == 'urgent' else None
                    if expected_label and expected_label not in probe.controls.get('evaluation_labels', []):
                        checks[-1]['execution_gap'] = 'urgent_profile_present_but_no_route_policy_for_label'
                    if expected_label:
                        from app.automation_models import AutomationRun
                        probe.engine_version = 'beijing-test'
                        probe.mode = 'journey'
                        probe.virtual_now = row.virtual_now
                        probe.generation = 1
                        probe.memory = {}
                        db.add(probe)
                        db.flush()
                        run = AutomationRun(session_id=probe.id, generation=1, module='journey', idempotency_key=f'urgent-probe-{probe.id}', status='running', trace={})
                        db.add(run)
                        db.flush()
                        service.apply_decision(db, probe, run, bundle, decision)
                        assert expected_label in probe.controls.get('evaluation_labels', [])
                        assert probe.controls['route_variant'] == ''
                        assert service.state(probe)['handoff'] is True
                        assert not probe.messages
                        checks[-1]['custom_handoff_label_execution_verified'] = True
            except Exception as exc:
                checks.append({'case': case['id'], 'error': type(exc).__name__ + ':' + str(exc)})
        report = {**result, 'idempotent': True, 'other_configuration_preserved': True,
                  'asset_resolution': 40, 'introduction_parts': len(intro_parts), 'intake_seconds': 30,
                  'followup_rules_rendered': followup_checks, 'permission_stops_verified': True,
                  'rendering': checks, 'production_modified': False}
        (OUT / 'integration.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps({**result, 'render_errors': [c for c in checks if 'error' in c],
                          'execution_gaps': [{'case': c['case'], 'gap': c['execution_gap']}
                                             for c in checks if 'execution_gap' in c]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
