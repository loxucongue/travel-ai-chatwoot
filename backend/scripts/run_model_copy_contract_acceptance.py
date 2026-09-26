"""Model-only semantic contract probes; synthetic facts, no DB writes or sender."""
import json
import sys
from dataclasses import replace, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.reply_generation import GeneratedReply, GeneratedFollowUp
from app.reply_fact_verification import call_reply_fact_verifier
from app.reply_planning import build_reply_plan, FollowUp
from app.reply_understanding import CustomerUnderstanding
from app.route_packages import JOURNEY_POLICY
from app.release_provenance import source_fingerprint

context = {"customer_text": "機票可以代訂嗎？", "reception_policy": JOURNEY_POLICY,
           "global_knowledge_facts": [{"id": "web.contract.fixture", "text":
               "機票可自行購買，顧問也可提供航班建議及協助代訂。房間配有獨立衛浴和供氧設備。"}]}
plan = build_reply_plan(context, CustomerUnderstanding(intent="transport", customer_questions=["transport"]))
plan = replace(plan, action="reply", follow_up=None, allowed_asset_ids=[],
               allowed_fact_ids=["web.contract.fixture"], allowed_content_group_keys=[],
               fixed_answer_id="", reply_goal="直接回答客戶目前的問題")
cases = [
    ("service_statement", "機票可以代訂嗎？", "可以，顧問能提供航班建議，也能協助代訂。", True),
    ("self_booking", "機票可以自己訂嗎？", "可以自行購買機票，也能請顧問協助代訂喔。", True),
    ("unpunctuated_request", "機票可以代訂嗎？", "可以協助代訂。請您告訴我人數和出發日期。", False),
    ("question", "機票可以代訂嗎？", "可以協助代訂。您幾位、何時出發呢？", False),
    ("contact_request", "機票可以代訂嗎？", "可以協助代訂。方便留下LINE，我把資料傳給您。", False),
    ("unsent_image", "房間有什麼設備？", "房間有獨立衛浴和供氧設備，我把照片傳給您看。", False),
    ("guarantee", "供氧設備有什麼用途？", "房間有供氧設備，保證不會高反。", False),
    ("social_proof", "房間有什麼設備？", "房間有供氧設備，很多客人都說住起來最安全。", False),
    ("unrelated", "機票可以代訂嗎？", "房間有獨立衛浴和供氧設備。", False),
    ("simplified", "機票可以代訂嗎？", "可以代订机票，这边会帮您对接航班建议。", False),
    ("valid_facilities", "房間有什麼設備？", "房間配有獨立衛浴和供氧設備喔。", True),
    ("legal_followup", "機票可以代訂嗎？", "可以，顧問能提供航班建議，也能協助代訂。", True),
]
results = []
fingerprint = source_fingerprint()
for key, question, body, expected in cases:
    local = {**context, "customer_text": question}
    current_plan = replace(plan, follow_up=FollowUp("contact", "line", "方便留個LINE嗎？")) if key == 'legal_followup' else plan
    followup = GeneratedFollowUp("contact", "line", current_plan.follow_up.question) if current_plan.follow_up else None
    generated = GeneratedReply(body, followup, plan.allowed_fact_ids, [])
    checked, logs, digest = call_reply_fact_verifier(local, current_plan, generated)
    result = {"key": key, "question": question, "body": body, "expected_accept": expected,
              "verification": asdict(checked), "model_calls": logs, "digest": digest,
              "passed": (checked.supported and checked.relevant) == expected}
    results.append(result)
    print(json.dumps({"key": key, "passed": result["passed"]}), flush=True)
assert fingerprint == source_fingerprint()
report = {"source_fingerprint": fingerprint, "synthetic_facts": True,
          "passed": all(row["passed"] for row in results), "cases": results}
Path("model-copy-contract.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
raise SystemExit(0 if report["passed"] else 1)
