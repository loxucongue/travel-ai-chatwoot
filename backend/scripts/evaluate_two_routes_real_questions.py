"""Build and optionally run isolated DeepSeek replay suites from real questions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.models import Tenant
from app.two_route_real_replay import (
    assert_evaluation_only,
    audit_route_packages,
    build_test_suites,
    catalog_row,
    collect_real_turns,
    outbound_count,
    public_case_row,
    report_markdown,
    run_case,
    summarize_results,
    write_json,
)
from app.material_library import candidate_materials
from sqlalchemy import select


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=root / "output" / "two-route-real-replay-20260830")
    parser.add_argument("--inbox-id", type=int, default=128859)
    parser.add_argument("--per-route", type=int, default=24)
    parser.add_argument("--safety-limit", type=int, default=12)
    parser.add_argument("--long-context-limit", type=int, default=12)
    parser.add_argument("--lead-limit", type=int, default=8)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Limit model cases after suites are built.")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--case-id", action="append", default=[], help="Run only selected stable case IDs.")
    return parser.parse_args()


def load_completed(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            # Resume only successful cases. Infrastructure/model failures must
            # be retried instead of being treated as completed release evidence.
            if item.get("status") == "passed":
                rows[item["case_id"]] = item
    return rows


def main() -> int:
    args = parse_args()
    assert_evaluation_only()
    args.output.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        before = outbound_count(db)
        turns, snapshot = collect_real_turns(db, args.inbox_id, {26})
        tenant = db.scalar(select(Tenant).order_by(Tenant.id))
        materials = candidate_materials(db, tenant.id if tenant else None)
    suites = build_test_suites(
        turns,
        per_route=args.per_route,
        safety_limit=args.safety_limit,
        long_context_limit=args.long_context_limit,
        lead_limit=args.lead_limit,
    )
    route_audit = audit_route_packages()
    with (args.output / "catalog.jsonl").open("w", encoding="utf-8") as handle:
        for item in turns:
            handle.write(json.dumps(catalog_row(item), ensure_ascii=False) + "\n")
    write_json(args.output / "snapshot.json", snapshot)
    write_json(args.output / "route-package-audit.json", route_audit)
    for name, cases in suites.items():
        write_json(args.output / "suites" / f"{name}.json", [public_case_row(item) for item in cases])
    all_cases = [item for cases in suites.values() for item in cases]
    if args.case_id:
        requested = set(args.case_id)
        all_cases = [item for item in all_cases if item.case_id in requested]
        missing = requested - {item.case_id for item in all_cases}
        if missing:
            raise SystemExit(f"unknown case ids: {sorted(missing)}")
    if args.limit:
        all_cases = all_cases[: args.limit]
    build_summary = {
        "snapshot": snapshot,
        "route_package_audit": route_audit,
        "suite_counts": {key: len(value) for key, value in suites.items()},
        "selected_model_cases": len(all_cases),
        "available_materials": len(materials),
        "outbound_rows_before": before,
        "chatwoot_write_requests": 0,
    }
    write_json(args.output / "build-summary.json", build_summary)
    print(json.dumps({
        "phase": "built",
        "output": str(args.output),
        "turns": len(turns),
        "cases": len(all_cases),
        "suites": build_summary["suite_counts"],
        "route_audit": route_audit["passed"],
        "outbound_requests": 0,
    }, ensure_ascii=False), flush=True)
    if args.build_only:
        return 0

    result_path = args.output / "results.jsonl"
    completed = {} if args.no_resume else load_completed(result_path)
    if args.no_resume and result_path.exists():
        result_path.unlink()
    for index, case in enumerate(all_cases, 1):
        if case.case_id in completed:
            continue
        result = run_case(case, materials)
        with result_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
        completed[case.case_id] = result
        print(json.dumps({
            "phase": "replay",
            "index": index,
            "total": len(all_cases),
            "suite": case.suite,
            "status": result["status"],
        }, ensure_ascii=False), flush=True)

    ordered = [completed[item.case_id] for item in all_cases if item.case_id in completed]
    with SessionLocal() as db:
        after = outbound_count(db)
    summary = summarize_results(ordered, route_audit, snapshot, before, after)
    write_json(args.output / "summary.json", summary)
    (args.output / "report.md").write_text(report_markdown(summary, ordered), encoding="utf-8")
    print(json.dumps({
        "phase": "complete",
        "results": len(ordered),
        "model_completed": summary["model_replay"]["model_completed"],
        "business_rule_pass_rate": summary["model_replay"]["business_rule_pass_rate"],
        "infrastructure_failed": summary["model_replay"]["infrastructure_failed"],
        "outbound_unchanged": summary["safety"]["outbound_rows_unchanged"],
        "report": str(args.output / "report.md"),
    }, ensure_ascii=False), flush=True)
    replay = summary["model_replay"]
    release_gate_passed = all((
        route_audit["passed"],
        len(ordered) == len(all_cases),
        replay["passed"] == len(all_cases),
        replay["infrastructure_failed"] == 0,
        replay["model_output_failed"] == 0,
        summary["safety"]["outbound_rows_unchanged"],
        summary["safety"]["chatwoot_write_requests"] == 0,
    ))
    return 0 if release_gate_passed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
