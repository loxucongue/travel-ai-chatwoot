"""Reconcile the audited Oct 7 handoffs; never replay messages or followups.

Default is a preview. --apply writes a snapshot and reconciles only unchanged
audited conversations. This is a one-off incident repair, not worker startup.
"""
import argparse
import json
import sys
from pathlib import Path
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal
from app.models import ConversationState, MessageEvent, OutboundMessage, HandoffTask, utcnow
from app.reception_v3.live import record_human_reply, session_for
from app.chatwoot_service import client_for
from app.models import ChatwootConnection
from app.operations import audit


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    expected={300:906086566,306:905515202,307:905829225,308:906060156}
    results=[]
    with SessionLocal() as db:
        before=[]
        # Snapshot the whole incident set before any same-contact reconciliation.
        for conv in db.scalars(select(ConversationState).where(ConversationState.chatwoot_conversation_id.in_(expected))):
            row=session_for(db,conv.id)
            before.append({'conversation':conv.chatwoot_conversation_id,'state':conv.effective_ai_state,'reason':conv.effective_state_reason,
                'session_id':row.id if row else None,'controls':row.controls if row else None,
                'tasks':[{k:getattr(t,k) for k in ('id','status','claimed_at','sla_due_at','chatwoot_assignee_id','version')}
                         for t in db.scalars(select(HandoffTask).where(HandoffTask.conversation_state_id==conv.id))]})
        if args.apply:
            folder=Path('data/incident-backups');folder.mkdir(exist_ok=True)
            backup=folder/('oct08-'+utcnow().replace(':','-')+'.json')
            backup.write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf8')
        for cid,mid in expected.items():
            conv=db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id==cid))
            if not conv:continue
            latest=db.scalar(select(MessageEvent).where(MessageEvent.conversation_state_id==conv.id,
                MessageEvent.private.is_(False),MessageEvent.direction.in_(['incoming','outgoing']))
                .order_by(MessageEvent.chatwoot_message_id.desc()))
            if not latest or latest.chatwoot_message_id!=mid:
                results.append({'conversation':cid,'skipped':'new_messages_since_audit'});continue
            if db.scalar(select(OutboundMessage.id).where(OutboundMessage.chatwoot_message_id==mid)):
                raise RuntimeError('audited_human_message_is_ai')
            if args.apply:record_human_reply(db,conv,latest)
            results.append({'conversation':cid,'human_message_id':mid,'action':'reconcile_human_ownership'})
        if args.apply:
            db.commit()
        # Restore only the exact mistaken reassignment proved by notification 9.
        connection=db.scalar(select(ChatwootConnection))
        client=client_for(connection)
        try:
            remote=client.get_conversation(307)
            history=client.get_messages(307)
            last=max((m['id'] for m in history.get('payload',[])),default=0)
            owner=(remote.get('meta',{}).get('assignee') or {}).get('id')
            if owner==264715 and last==905872639:
                if args.apply:
                    client.assign_conversation(307,275471)
                    conv=db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id==307))
                    conv.assignee_id,conv.assignee_name=275471,'Amber'
                    audit(db,None,'incident.restore_advisor','conversation',307,
                          {'previous_agent_id':264715,'agent_id':275471,'notification_id':9})
                    db.commit()
                results.append({'conversation':307,'action':'restore_owner','from':264715,'to':275471})
            else:results.append({'conversation':307,'action':'preserve_current_owner','agent_id':owner})
        finally:client.close()
    print(json.dumps({'applied':args.apply,'changes':results,'messages_sent':0,'historical_followups_replayed':0},ensure_ascii=False))


if __name__=='__main__':main()
