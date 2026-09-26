"""Local read-only replay utilities. Never runs the customer sending worker."""
import argparse
import json
import time
from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.models import SyncJob, Tenant, User, EvaluationDataset, EvaluationRun, EvaluationResult, EvaluationCase, OutboundMessage, HandoffTask, MessageEvent, ConversationState
from app.evaluation_sync import process_evaluation_sync
from app.evaluation_dataset import build_dataset
from app.evaluation_service import create_run, process_next_result
from pathlib import Path
from sqlalchemy import func


def sync():
    assert settings.app_profile == "evaluation" and not settings.outbound_enabled
    with SessionLocal() as db:
        job = db.scalar(select(SyncJob).where(SyncJob.kind == "evaluation_history", SyncJob.status.in_(["pending", "running"])))
        if not job:
            job = SyncJob(tenant_id=db.scalar(select(Tenant)).id, kind="evaluation_history", phase="backup")
            db.add(job)
            db.commit()
        job_id = job.id
    errors = 0
    while True:
        with SessionLocal() as db:
            job = db.get(SyncJob, job_id)
            if job.status not in ("pending", "running"):
                print(json.dumps({"job": job_id, "status": job.status, "completed": job.completed_items, "failed": job.failed_items}), flush=True)
                return
            try:
                process_evaluation_sync(db)
                errors = 0
            except Exception as exc:
                errors += 1
                print(json.dumps({"error": type(exc).__name__, "retry": errors}), flush=True)
                if errors >= 5:
                    raise
                time.sleep(10)
            print(json.dumps({"job": job_id, "page": job.current_page, "completed": job.completed_items, "phase": job.phase}), flush=True)
        time.sleep(.15)


def replay():
    assert settings.app_profile=="evaluation" and settings.outbound_mode=="disabled" and not settings.chatwoot_write_enabled
    with SessionLocal() as db:
        user=db.scalar(select(User).where(User.role.in_(["admin","super_admin"])))
        for module in ("reply","wakeup","sop"):
            name=f"three-modules-20260826-{module}-v2"
            dataset=db.scalar(select(EvaluationDataset).where(EvaluationDataset.name==name))
            if not dataset:
                dataset=build_dataset(db,user,name,settings.evaluation_inbox_id,module,True)
            run=create_run(db,dataset,user)
            db.commit()
            print(json.dumps({"module":module,"dataset":dataset.id,"run":run.id,"cases":run.total_cases}),flush=True)
    while True:
        with SessionLocal() as db:
            if not process_next_result(db):break
            rows=db.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(2)).all()
            print(json.dumps([{"id":r.id,"status":r.status,"completed":r.completed_cases,"failed":r.failed_cases,"total":r.total_cases} for r in rows]),flush=True)
    export_report()


def export_report():
    from app.automation_api import safe_text
    target=Path("../output/three-modules-20260826")
    target.mkdir(parents=True,exist_ok=True)
    with SessionLocal() as db:
        datasets=db.scalars(select(EvaluationDataset).where(EvaluationDataset.name.like("three-modules-20260826-%"))).all()
        reports=[]
        for dataset in datasets:
            run=db.scalar(select(EvaluationRun).where(EvaluationRun.dataset_id==dataset.id).order_by(EvaluationRun.id.desc()))
            if not run:continue
            cases=[]
            for result,case in db.execute(select(EvaluationResult,EvaluationCase).join(EvaluationCase,EvaluationResult.case_id==EvaluationCase.id).where(EvaluationResult.run_id==run.id)).all():
                cases.append(safe_text({"case_id":case.id,"conversation_id":case.conversation_state_id,"question":case.customer_text,"context":case.context_messages,"reference":case.reference_answer,"status":result.status,"draft":result.reply,"branch":result.branch,"action":result.action,"slots":result.slots,"evidence":result.evidence_refs,"safety":result.safety_flags,"review":result.review or {"status":"pending"},"trace":result.automatic_scores,"error":result.error_code}))
            report={"module":dataset.filter_config.get("module"),"dataset_id":dataset.id,"run_id":run.id,"snapshot_at":dataset.snapshot_at,"snapshot":dataset.filter_config,"status":run.status,"metrics":run.metrics,"cases":cases}
            reports.append(report)
            (target/f'{report["module"]}-cases.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        counts={t:db.scalar(select(func.count()).select_from(cls)) for t,cls in [("conversations",ConversationState),("messages",MessageEvent),("outbound",OutboundMessage),("handoffs",HandoffTask)]}
        summary={"safety":{"profile":settings.app_profile,"outbound_mode":settings.outbound_mode,"chatwoot_write_enabled":settings.chatwoot_write_enabled},"counts":counts,"modules":[{k:v for k,v in x.items() if k!="cases"} for x in reports]}
        (target/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        lines=["# 三板块全量回放报告", "", "## 范围与安全", f"冻结历史：{counts['conversations']} 个会话，{counts['messages']} 条消息。",f"当前真实出站记录 {counts['outbound']} 条，人工任务 {counts['handoffs']} 条；基线分别为 2、0。", "Chatwoot 只读、出站禁用。回放没有使用后续参考答案或预期分支作为输入。", "", "## 模型回放"]
        for x in reports:
            lines.extend([f"### {x['module']}",f"快照：{x['snapshot_at']}；状态：{x['status']}；{len(x['cases'])} 个案例。", "```json",json.dumps(x['metrics'],ensure_ascii=False,indent=2),"```",f"排除统计：{json.dumps(x['snapshot'].get('exclusions'),ensure_ascii=False)}",""])
        lines.extend(["## 解释边界", "- 结构有效率不等于业务准确率，引用存在也不证明事实完整正确。", "- 历史人工回答仅供对照，不是资料真值；未人工复核的案例全部待复核。", "- 唤醒以确认回复后两小时重建，仅使用该时间点前消息；当时标签、权限无法完整回溯，明确按模拟条件处理。", "- SOP 固定审核内容不调用模型；虚拟时钟验收见测试与 SOP 报告。", "- 原始旅游图片、真实客户实发和实际转化提升尚未验收。"])
        (target/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print(str(target.resolve()),flush=True)


def smoke():
    from app.decision_service import generate_decision
    from dataclasses import asdict
    decision,logs,digest,trace=generate_decision({"customer_text":"我們兩位，想了解桃花9日，不去珠峰。","context_messages":[]})
    print(json.dumps({"decision":asdict(decision),"trace":trace},ensure_ascii=False))
    assert decision.slots.get("party_size")==2
    assert decision.branch=="peach_9d"
    assert "几人" not in (decision.reply or "") and "幾人" not in (decision.reply or "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["sync","replay","report","smoke"])
    args = parser.parse_args()
    {"sync":sync,"replay":replay,"report":export_report,"smoke":smoke}[args.action]()
