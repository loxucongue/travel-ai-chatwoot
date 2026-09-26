"""Replay frozen historical cases through the decision chain; never send messages."""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import fields
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from app.db import SessionLocal
from app.material_library import candidate_materials
from app.models import Tenant
from app.outbound_control import global_message_sending_enabled
from app.reception_config import effective_reception_policy
from app.release_provenance import source_fingerprint, assert_source_unchanged, runtime_config_fingerprint
from app.two_route_real_replay import (
    ReplayCase, run_case, outbound_count, assert_evaluation_only,
    restore_masked_case_input, reception_policy_fingerprint,
)


def _run_independent_case(case, materials, fingerprint, config_start, reception_policy=None,
                          input_provenance=None, website_knowledge=False):
    assert_source_unchanged(fingerprint)
    with SessionLocal() as db:
        if global_message_sending_enabled(db):
            raise RuntimeError("real_sending_must_stay_off")
        case_config_start = runtime_config_fingerprint(db)
        knowledge_context = None
        if website_knowledge:
            from app.web_knowledge import enrich_context_with_web_knowledge
            knowledge_context = enrich_context_with_web_knowledge(db, db.scalar(select(Tenant.id).order_by(Tenant.id)),
                {"customer_text": case.customer_text}, environment="playground")
    result = run_case(deepcopy(case), deepcopy(materials), reception_policy=deepcopy(reception_policy),
                      **({"knowledge_context": knowledge_context} if website_knowledge else {}))
    result['knowledge_environment'] = 'playground' if website_knowledge else 'legacy_route_only'
    with SessionLocal() as db:
        case_config_end = runtime_config_fingerprint(db)
    result['release_provenance'] = {
        'source_fingerprint': fingerprint,
        'runtime_config_fingerprint_start': case_config_start,
        'runtime_config_fingerprint_end': case_config_end,
        'runtime_config_unchanged': case_config_start == case_config_end,
        'matches_run_start': config_start == case_config_start == case_config_end,
        'reception_policy_fingerprint': reception_policy_fingerprint(reception_policy)
            if reception_policy is not None else result.get('reception_policy_fingerprint'),
    }
    policy_digest = result['release_provenance']['reception_policy_fingerprint']
    result['release_provenance']['reception_policy_matches_run_start'] = (
        result.get('reception_policy_fingerprint') == policy_digest)
    if input_provenance is not None:
        result['input_provenance'] = deepcopy(input_provenance)
    return result


def _case_results(cases, materials, fingerprint, config_start, workers, reception_policy=None,
                  input_provenance=None, website_knowledge=False):
    input_provenance = input_provenance or {}
    if workers == 1:
        for case in cases:
            yield _run_independent_case(case, materials, fingerprint, config_start,
                                        reception_policy, input_provenance.get(case.case_id), website_knowledge)
        return
    # Keep at most workers cases submitted, not an unbounded executor queue.
    remaining = iter(cases)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="history-replay") as executor:
        pending = set()
        for _ in range(workers):
            case = next(remaining, None)
            if case is not None:
                pending.add(executor.submit(_run_independent_case, case, materials, fingerprint, config_start,
                                            reception_policy, input_provenance.get(case.case_id), website_knowledge))
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield future.result()
                case = next(remaining, None)
                if case is not None:
                    pending.add(executor.submit(_run_independent_case, case, materials, fingerprint, config_start,
                                                reception_policy, input_provenance.get(case.case_id), website_knowledge))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suites", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1,
                        help="Independent replay workers (1-4); default: 1.")
    parser.add_argument("--restore-masked-inputs", action="store_true",
                        help="Recover uniquely verified original current turns in memory only.")
    parser.add_argument("--website-knowledge", action="store_true", help="Use the same playground website enrichment as the normal executor; keep frozen questions unchanged.")
    args = parser.parse_args()
    assert_evaluation_only()
    fingerprint = source_fingerprint()
    names = {field.name for field in fields(ReplayCase)}
    cases = []
    for path in sorted(args.suites.glob("*.json")):
        cases.extend(ReplayCase(**{key: value for key, value in row.items() if key in names})
                     for row in json.loads(path.read_text(encoding="utf-8")))
    if len(cases) != 68 or len({case.case_id for case in cases}) != 68:
        raise RuntimeError("expected_68_unique_frozen_cases")
    with SessionLocal() as db:
        config_start = runtime_config_fingerprint(db)
        if global_message_sending_enabled(db):
            raise RuntimeError("real_sending_must_stay_off")
        before = outbound_count(db)
        materials = candidate_materials(db, db.scalar(select(Tenant.id).order_by(Tenant.id)))
        with db.no_autoflush:
            reception_policy = deepcopy(effective_reception_policy(db))
            policy_digest = reception_policy_fingerprint(reception_policy)
            input_provenance = {}
            if args.restore_masked_inputs:
                restored = []
                for case in cases:
                    copy, provenance = restore_masked_case_input(db, case)
                    restored.append(copy)
                    input_provenance[case.case_id] = provenance
                cases = restored
    args.output.mkdir(parents=True, exist_ok=True)
    result_path = args.output / "results.jsonl"
    if result_path.exists():
        raise RuntimeError("acceptance_output_already_exists")
    passed = 0
    config_unchanged = True
    policy_unchanged = True
    with result_path.open("x", encoding="utf-8") as stream:
        for index, result in enumerate(_case_results(
                cases, materials, fingerprint, config_start, args.workers,
                reception_policy, input_provenance, args.website_knowledge), 1):
            config_unchanged = config_unchanged and result['release_provenance']['matches_run_start']
            policy_unchanged = policy_unchanged and result['release_provenance']['reception_policy_matches_run_start']
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            passed += result["status"] == "passed"
            print(json.dumps({"case": index, "total": len(cases), "status": result["status"]}), flush=True)
    with SessionLocal() as db:
        unchanged = before == outbound_count(db) and not global_message_sending_enabled(db)
        config_end = runtime_config_fingerprint(db)
    assert_source_unchanged(fingerprint)
    report = {"passed": passed, "total": len(cases), "outbound_unchanged": unchanged,
              "knowledge_environment": "playground" if args.website_knowledge else "legacy_route_only",
              "workers": args.workers,
              "source_fingerprint": fingerprint,
              "runtime_config_fingerprint_start": config_start,
              "runtime_config_fingerprint_end": config_end,
              "runtime_config_unchanged": config_unchanged and config_start == config_end,
              "reception_policy_fingerprint": policy_digest,
              "reception_policy_unchanged": policy_unchanged,
              "restored_input_count": sum(value.get("restored", False) for value in input_provenance.values())}
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if passed == len(cases) and unchanged and policy_unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
