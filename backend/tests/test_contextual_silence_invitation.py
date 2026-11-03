from copy import deepcopy

import pytest

from app.reception_v3 import service
from app.reception_v3.lead_priority import validation_error
from test_lead_priority import context, invitation
from test_reception_v3 import create


def configured_context():
    value = context()
    value['skills']['lead_capture'].update(
        next_day_invitation_enabled=False, contextual_silence_invitation_enabled=True)
    value['messages'].append({'id':'old-invite', 'direction':'outgoing', 'status':'delivered',
                              'contact_invitation':True, 'content':'May I have your LINE?'})
    return value


def test_repeat_invitation_requires_explicit_business_opt_in():
    value = configured_context()
    assert validation_error(invitation(), value) is None
    for enabled in (False, None):
        value['skills']['lead_capture']['contextual_silence_invitation_enabled'] = enabled
        assert validation_error(invitation(), value)


@pytest.mark.parametrize('section,key,value', [
    ('profile','contact_capture_declined',True), ('profile','contact_value','known-id'),
    ('profile','contact_status','declined'), ('profile','contact_status','deferred'),
    ('profile','contact_status','provided'), ('profile','contact_status','attachment_pending_verification'),
    ('state','human_followup',True), ('state','handoff',True), ('state','opt_out',True),
])
def test_opt_in_cannot_override_customer_choice_or_human_ownership(section, key, value):
    ctx = configured_context()
    ctx[section][key] = value
    assert validation_error(invitation(), ctx)


@pytest.mark.parametrize('status,blocked', [('invited',False),('declined',True),('deferred',True),('provided',True)])
def test_historical_review_distinguishes_an_unanswered_invitation_from_refusal(status, blocked):
    decision = invitation()
    decision['lead_review']['contact_status'] = status
    assert bool(validation_error(decision, configured_context())) is blocked


@pytest.mark.parametrize('revoke', ['policy', 'capture', 'declined', 'deferred'])
def test_queued_contextual_invitation_is_cancelled_when_permission_changes(session_factory, monkeypatch, revoke):
    with session_factory() as db:
        row = create(db, monkeypatch)
        bundle = configured_context()['skills']
        _, parts = service.parts_for(db, row, bundle, invitation(), followup=True)
        assert parts[0]['contextual_silence_invitation']
        assert parts[0]['contact_invitation']
        part = {**parts[0], 'id':'pending-contact', 'status':'draft'}
        row.messages = [part]
        monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(bundle))
        assert not service.skip_revoked_contact(db, row, part)
        if revoke == 'policy':
            bundle['lead_capture']['contextual_silence_invitation_enabled'] = False
        elif revoke == 'capture':
            bundle['lead_capture']['enabled'] = False
        else:
            row.memory = {'contact_status': revoke}
        assert service.skip_revoked_contact(db, row, part)
        assert row.messages[0]['status'] == 'cancelled'


def test_contextual_setting_api_roundtrip_preserves_schedule_and_scope(authenticated):
    client, csrf = authenticated
    url = '/v1/automation/reception-config'
    original = client.get(url).json()['config']
    for enabled in (True, False):
        response = client.patch(url, json={'lead_capture':{'contextual_silence_invitation_enabled':enabled}},
                                headers={'X-CSRF-Token':csrf})
        assert response.status_code == 200, response.text
        config = client.get(url).json()['config']
        assert config['lead_capture']['contextual_silence_invitation_enabled'] is enabled
        assert config['silence'] == original['silence']
        assert config['routing'] == original['routing']
