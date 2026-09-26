"""Run a read-only V2 quality and latency preview with real model calls."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.decision_service import generate_decision
from app.config import settings


CASES = [
    {"key": "price_9d", "customer_text": "\u6843\u82b19\u65e5\u4e00\u500b\u4eba\u591a\u5c11\u9322\uff1f", "context_messages": []},
    {"key": "oxygen_followup", "customer_text": "\u81ea\u5df1\u8981\u6e96\u5099\u6c27\u6c23\u55ce\uff1f", "context_messages": [
        {"role": "customer", "content": "\u6843\u82b111\u65e5\u4e0b\u8eca\u8d70\u884c\u7a0b\u4e5f\u6709\u6c27\u6c23\u55ce\uff1f"},
        {"role": "assistant", "content": "\u524d\u5f805000\u516c\u5c3a\u4ee5\u4e0a\u666f\u9ede\u6642\uff0c\u6703\u63d0\u4f9b\u96a8\u8eab\u6c27\u6c23\u74f6\u3002"},
    ], "route_variant": "peach_11d_2027"},
    {"key": "considering", "customer_text": "\u6211\u5148\u8ddf\u5bb6\u4eba\u8a0e\u8ad6", "context_messages": [
        {"role": "customer", "content": "\u6843\u82b19\u65e5\u591a\u5c11\u9322\uff1f"},
        {"role": "assistant", "content": "9\u65e5\u884c\u7a0b\u6bcf\u4eba\u4eba\u6c11\u5e639,980\u5143\u3002"},
    ], "route_variant": "peach_9d_2027"},
    {"key": "route_comparison", "customer_text": "9\u65e5\u548c11\u65e5\u4e3b\u8981\u5dee\u5728\u54ea\u91cc\uff1f", "context_messages": []},
    {"key": "discount_unknown", "customer_text": "\u5982\u679c\u4eca\u5929\u8ba2\u53ef\u4ee5\u518d\u4f18\u60e0\u4e24\u5343\u5417\uff1f", "context_messages": [], "route_variant": "peach_9d_2027"},
    {"key": "wechat_handoff", "customer_text": "\u6211\u7684\u5fae\u4fe1\u662f travel_test_88\uff0c\u8bf7\u987e\u95ee\u52a0\u6211", "context_messages": [], "route_variant": "peach_11d_2027"},
    {"key": "large_group_handoff", "customer_text": "\u6211\u4eec8\u4e2a\u4eba\uff0c\u60f3\u505a\u79c1\u4eba\u56e2\u62a5\u4ef7", "context_messages": []},
    {"key": "explicit_stop", "customer_text": "\u4e0d\u7528\u4e86\uff0c\u8bf7\u4e0d\u8981\u518d\u8054\u7cfb\u6211", "context_messages": [], "route_variant": "peach_9d_2027"},
    {"key": "silence_considering", "module": "silence_touch", "customer_text": "\u5ba2\u6237\u5df2\u6c89\u9ed824\u5c0f\u65f6", "context_messages": [
        {"role": "customer", "content": "\u6211\u5148\u8ddf\u5bb6\u4eba\u5546\u91cf\uff0c\u6709\u9700\u8981\u518d\u627e\u4f60"},
        {"role": "assistant", "content": "\u597d\u7684\uff0c\u60a8\u5148\u8ba8\u8bba\uff0c\u4e0d\u6025\u3002"},
    ], "journey": {"stage": "considering", "sent_content_groups": []}, "touch_index": 1},
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", dest="case_keys")
    parser.add_argument("--engine", choices=["v1", "v2"], default="v2")
    args = parser.parse_args()
    rows = []
    selected = [case for case in CASES if not args.case_keys or case["key"] in set(args.case_keys)]
    for case in selected:
        started = time.monotonic()
        try:
            decision, logs, _, trace = generate_decision({"module": "reply", "engine_version": args.engine, **case})
            rows.append({
                "key": case["key"], "input": case["customer_text"], "action": decision.action,
                "reply": decision.reply, "route_variant": decision.route_variant,
                "lead_action": decision.lead_action, "handoff_reason": decision.handoff_reason,
                "wakeup_action": decision.wakeup_action, "safety_flags": decision.safety_flags,
                "evidence_refs": decision.evidence_refs, "loaded_skills": trace.get("loaded_skills", []),
                "tools": trace.get("tools", []), "total_ms": trace.get("total_ms"),
                "request_count": len(logs), "outbound": False,
                "flow": trace.get("flow"), "release": trace.get("engine_release_id"),
                "model_request_ms": trace.get("model_request_ms"),
                "verification_ms": trace.get("verification_ms"),
                "repair_ms": trace.get("repair_ms"),
            })
        except Exception as exc:
            rows.append({"key": case["key"], "input": case["customer_text"], "error": str(exc)[:160], "outbound": False})
        rows[-1]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"engine_version": args.engine, "model": settings.deepseek_model,
                "cases": rows, "outbound": False}, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {"engine_version": args.engine, "model": settings.deepseek_model, "cases": rows, "outbound": False}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=True))


if __name__ == "__main__":
    main()
