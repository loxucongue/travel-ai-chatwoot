"""Re-evaluate only cases affected by the customer-evidence validation fix."""
import json
from pathlib import Path
import re
from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.models import EvaluationCase, EvaluationResult, EvaluationRun, EvaluationDataset
from app.decision_service import party_quote_eligible, GUARD_VERSION
from app.evaluation_service import process_next_result, SAFE_HANDOFF_REPLY
from app.replay_cli import export_report


def main():
    assert settings.app_profile=='evaluation' and not settings.outbound_enabled
    target=Path('../output/three-modules-20260826/targeted-retest.json')
    if not target.exists():
        affected=[]
        with SessionLocal() as db:
            rows=db.execute(select(EvaluationResult,EvaluationCase,EvaluationRun).join(EvaluationCase,EvaluationResult.case_id==EvaluationCase.id).join(EvaluationRun,EvaluationResult.run_id==EvaluationRun.id).join(EvaluationDataset,EvaluationCase.dataset_id==EvaluationDataset.id).where(EvaluationDataset.name.like('three-modules-20260826-%'))).all()
            for result,case,run in rows:
                reason=None
                quote=(result.automatic_scores.get('slot_evidence') or {}).get('party_size')
                customers=[m.get('content','') for m in case.context_messages if m.get('direction')=='incoming']+[case.customer_text]
                lines=[line for text in customers for line in text.splitlines()]
                if result.slots.get('party_size') and quote and not any(quote in line and party_quote_eligible(line) for line in lines):reason='product_capacity_not_party_size'
                if result.action=='reply' and re.search(r'最[適适]合在|最佳[赏賞]花|桃花盛[開开]|限定在\d|花期(?:是|為|为)',result.reply or ''):reason='unverified_season_claim'
                if reason:
                    affected.append({'result_id':result.id,'case_id':case.id,'reason':reason,'previous_status':result.status})
                    result.status,result.error_code,result.completed_at='pending',None,None
                    run.status,run.completed_at='running',None
                elif result.reply=='這項需求需要由旅遊顧問確認，我先為您記錄並轉交人工處理。':
                    result.reply=SAFE_HANDOFF_REPLY
                    result.automatic_scores={**result.automatic_scores,'handoff_wording_patch':GUARD_VERSION}
            db.commit()
        target.write_text(json.dumps({'guard_version':GUARD_VERSION,'affected_count':len(affected),'cases':affected},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'targeted_cases':len(affected)}),flush=True)
    done=0
    while True:
        with SessionLocal() as db:
            if not process_next_result(db):break
        done+=1
        print(json.dumps({'patched':done}),flush=True)
    export_report()


if __name__=='__main__':main()
