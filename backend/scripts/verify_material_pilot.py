"""Small paid DeepSeek regression sample. Read history, never send to Chatwoot."""
import json
import argparse
import sys
from dataclasses import asdict
from pathlib import Path
from statistics import median

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select,func
from app.config import settings
from app.db import SessionLocal
from app.models import MessageEvent, OutboundMessage, InboxBinding, ConversationState
from app.material_library import candidate_materials, resolve_materials
from app.decision_service import generate_decision
from app.automation_api import safe_text


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--only",default="")
    parser.add_argument("--recheck",action="store_true")
    args=parser.parse_args()
    if settings.outbound_mode != "disabled" or settings.chatwoot_write_enabled:
        raise RuntimeError("evaluation_only")
    output=Path(__file__).resolve().parents[2]/"output/material-pilot-verification"
    output.mkdir(exist_ok=True)
    prior=[{"direction":"incoming","content":"我想了解2027年桃花9日行程，不上珠峰。"}]
    cases=[
        ("9日行程", "想看2027年桃花9日的行程圖片，不上珠峰。", [], "", "reply", "itinerary"),
        ("11日行程", "想看2027年桃花加珠峰11日的行程圖。", [], "", "reply", "itinerary"),
        ("住宿追问", "住宿房間有照片可以看看嗎？", prior, "peach_9d_2027", "reply", "room"),
        ("车辆追问", "車子裡面長什麼樣子，有照片嗎？", prior, "peach_9d_2027", "reply", "vehicle"),
        ("景点追问", "想看看桃花景點照片。", prior, "peach_9d_2027", "reply", ""),
        ("医疗边界", "有高血壓，吸氧是不是就保證不會高反？", prior, "peach_9d_2027", "handoff", ""),
        ("冬游边界", "冬天8日上珠峰，有行程圖嗎？", [], "", "handoff", ""),
        ("月份歧义", "9月想去西藏，能看看行程嗎？", [], "", None, ""),
    ]
    historical=[(817193710,"真实：泛西藏咨询",None),(817193424,"真实：川西11天","handoff"),
                (816787257,"真实：四人一车问价","handoff"),(815102344,"真实：费用","handoff")]
    with SessionLocal() as db:
        inbox=db.scalar(select(InboxBinding).where(InboxBinding.chatwoot_inbox_id==128859))
        before={"conversations":db.scalar(select(func.count(ConversationState.id))),"messages":db.scalar(select(func.count(MessageEvent.id))),"outbound":db.scalar(select(func.count(OutboundMessage.id)))}
        for mid,name,expected in historical:
            row=db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id==mid,MessageEvent.direction=="incoming",MessageEvent.private.is_(False)))
            if row:cases.append((name,safe_text(row.content),[],"",expected,""))
        available=candidate_materials(db,inbox.tenant_id)
        tenant_id=inbox.tenant_id
    results=[]
    result_file=output/"deepseek-results.json"
    if args.recheck:
        results=json.loads(result_file.read_text(encoding="utf-8"))
        for entry in results:
            if entry["name"]=="月份歧义":
                decision=entry.get("decision",{})
                entry["expected_action"]="clarify_or_handoff_without_unverified_route"
                entry["passed"]=not entry.get("materials") and (decision.get("action")=="handoff" or
                    (decision.get("branch")=="unclassified" and not decision.get("route_variant") and "9日" not in decision.get("reply","") and "11日" not in decision.get("reply","")))
    if args.only and result_file.exists():
        original=result_file.read_text(encoding="utf-8")
        (output/"deepseek-initial-results.json").write_text(original,encoding="utf-8")
        results=[x for x in json.loads(original) if x["name"]!=args.only]
    for name,question,history,route,action,topic in cases:
        if args.recheck:continue
        if args.only and args.only!=name:continue
        entry={"name":name,"question":question,"expected_action":action,"known_route":route}
        try:
            decision,calls,digest,trace=generate_decision({"customer_text":question,"context_messages":history,"route_variant":route,"available_materials":available,"module":"reply"})
            with SessionLocal() as db:
                media=resolve_materials(db,decision.material_keys,decision.route_variant,tenant_id) if decision.material_keys else []
            passed=action is None or decision.action==action
            if topic:passed=passed and bool(media) and any(topic in x["content_family"] for x in media)
            if action=="handoff" or name=="月份歧义":passed=passed and not media
            if name=="月份歧义":passed=passed and (decision.action=="handoff" or (decision.branch=="unclassified" and not decision.route_variant and "9日" not in (decision.reply or "") and "11日" not in (decision.reply or "")))
            entry.update({"decision":safe_text(asdict(decision)),"materials":media,"trace":trace,"passed":passed})
        except Exception as exc:
            entry.update({"passed":False,"error":type(exc).__name__})
        results.append(entry)
        print(json.dumps({"case":name,"passed":entry["passed"],"action":entry.get("decision",{}).get("action"),"materials":len(entry.get("materials",[]))},ensure_ascii=True),flush=True)
        (output/"deepseek-results.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
    with SessionLocal() as db:
        after=db.scalar(select(func.count(OutboundMessage.id)))
    times=[x["trace"]["total_ms"] for x in results if "trace" in x]
    summary={"model":settings.deepseek_model,"sample":len(results),"scenario_assertions_passed":sum(x["passed"] for x in results),
        "latency_median_ms":median(times) if times else None,"latency_max_ms":max(times) if times else None,
        "request_count":sum(x.get("trace",{}).get("request_count",0) for x in results),
        "tokens":sum(x.get("trace",{}).get("input_tokens",0)+x.get("trace",{}).get("output_tokens",0) for x in results),
        "baseline":before,"outbound_after":after,"business_accuracy":None,"scope":"small unlabelled regression sample; not full acceptance"}
    assert after==before["outbound"]
    result_file.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
    (output/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=True))


if __name__=="__main__":main()
