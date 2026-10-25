from copy import deepcopy
import pytest
from app.reception_v3 import live,service,human_followup as hf
from app.models import ConversationState,MessageEvent,ConversationLabelSync
from test_human_return_20261016 import prepared,arm
from scripts.migrate_advisor_wait_20261017 import migrate_pending_wait


@pytest.mark.parametrize('case',['waiting','new_customer','draft'])
def test_upgrade_preserves_timer_and_rejects_changed_conversation(session_factory,monkeypatch,case):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task)
    with session_factory() as db:
        row=live.session_for(db,1)
        permission={k:v for k,v in row.controls['human_followup'].items() if k not in ('phase','resume_at')}
        row.controls={**row.controls,'human_followup':permission}
        before=deepcopy(row.controls)
        if case=='new_customer':db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=102,direction='incoming',content='question'))
        if case=='draft':row.messages=[*row.messages,{'status':'draft','content':'pending'}]
        db.flush()
        if case!='waiting':
            with pytest.raises(ValueError):migrate_pending_wait(db,row)
            assert row.controls==before
        else:
            result=migrate_pending_wait(db,row)
            assert result['next_check_at']==service.state(row)['next_check_at']
            assert row.controls['v3']==before['v3'] and row.controls['simulation']==before['simulation']
            assert hf.waiting(row) and row.controls['human_followup']['armed_at']==permission['armed_at']
            assert db.get(ConversationState,1).ai_mode=='disabled'
            assert db.get(ConversationLabelSync,1).target=='human'
            assert migrate_pending_wait(db,row) is None
