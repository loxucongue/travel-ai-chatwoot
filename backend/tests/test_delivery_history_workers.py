from contextlib import contextmanager
from threading import Barrier, Lock, get_ident
from types import SimpleNamespace
import sys

import pytest

from scripts import run_delivery_history_acceptance as runner


@pytest.mark.parametrize("workers", ["0", "5", "-1", "1.5", "abc"])
def test_invalid_workers_rejected_before_any_work(monkeypatch, workers):
    monkeypatch.setattr(sys, "argv", ["history", "--suites", "unused", "--output", "unused", "--workers", workers])
    monkeypatch.setattr(runner, "assert_evaluation_only", lambda: pytest.fail("validation must happen first"))
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2


@pytest.mark.parametrize("workers", [1, 3, 4])
def test_bounded_workers_isolate_context_and_sessions(monkeypatch, workers):
    lock = Lock()
    barrier = Barrier(workers)
    active = set()
    thread_ids = set()
    sessions = []
    maximum = 0
    main_thread = get_ident()

    @contextmanager
    def session_factory():
        db = SimpleNamespace(owner=get_ident(), closed=False)
        with lock:
            sessions.append(db)
        yield db
        db.closed = True

    def fingerprint(db):
        assert db.owner == get_ident() and not db.closed
        return "config"

    policy = {"operator": {"nested": []}}

    def run_case(case, materials, *, reception_policy=None):
        nonlocal maximum
        assert case.context == [] and materials == [{"nested": []}]
        assert reception_policy == policy
        policy_digest = runner.reception_policy_fingerprint(reception_policy)
        reception_policy["operator"]["nested"].append(case.case_id)
        case.context.append(case.case_id)
        materials[0]["nested"].append(case.case_id)
        with lock:
            active.add(case.case_id)
            thread_ids.add(get_ident())
            maximum = max(maximum, len(active))
        if int(case.case_id) < workers:
            barrier.wait(timeout=10)
        with lock:
            active.remove(case.case_id)
        return {"case_id": case.case_id, "status": "passed", "reception_policy_fingerprint": policy_digest}

    monkeypatch.setattr(runner, "SessionLocal", session_factory)
    monkeypatch.setattr(runner, "runtime_config_fingerprint", fingerprint)
    monkeypatch.setattr(runner, "global_message_sending_enabled", lambda db: False)
    monkeypatch.setattr(runner, "assert_source_unchanged", lambda expected: None)
    monkeypatch.setattr(runner, "run_case", run_case)
    cases = [SimpleNamespace(case_id=str(i), context=[]) for i in range(68)]
    materials = [{"nested": []}]
    results = []
    for result in runner._case_results(cases, materials, "source", "config", workers, policy):
        assert get_ident() == main_thread
        assert result["release_provenance"]["reception_policy_matches_run_start"]
        assert result["release_provenance"]["reception_policy_fingerprint"] == runner.reception_policy_fingerprint(policy)
        results.append(result)
    assert len(results) == 68
    assert {row["case_id"] for row in results} == {case.case_id for case in cases}
    assert maximum == workers
    assert len(thread_ids) == workers
    assert (main_thread in thread_ids) is (workers == 1)
    assert len(sessions) == 136 and len({id(db) for db in sessions}) == 136
    assert all(db.closed for db in sessions)
    assert all(case.context == [] for case in cases)
    assert materials == [{"nested": []}]
    assert policy == {"operator": {"nested": []}}


@pytest.mark.parametrize("restore", [False, True])
def test_runner_loads_effective_db_policy_once_and_reports_only_digest(
        session_factory, tmp_path, monkeypatch, capsys, restore):
    from dataclasses import asdict
    import hashlib
    import json
    from app.models import AppSetting, ConversationState, MessageEvent
    from app.config import settings
    from app.reception_config import SETTING_KEY, effective_reception_policy

    monkeypatch.setattr(settings, "live_sop_enabled", False)
    original = "WeChat ID: synthetic_fixture_only"
    with session_factory() as db:
        db.add(AppSetting(key=SETTING_KEY, value={"reply": {
            "max_characters": 137, "tone": "warm", "tone_guidance": "private operator tone"}}))
        db.commit()
        expected_policy = effective_reception_policy(db)
        state = ConversationState(tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=1)
        db.add(state)
        db.flush()
        db.add(MessageEvent(conversation_state_id=state.id, chatwoot_message_id=500,
                            direction="incoming", content=original))
        db.commit()
    policy_reads = []
    def read_policy(db):
        policy_reads.append(1)
        return effective_reception_policy(db)
    seen = []
    def run_case(case, materials, *, reception_policy=None):
        assert reception_policy == expected_policy
        assert reception_policy["reply_style"]["max_characters"] == 137
        if case.case_id == "0":
            assert case.customer_text == (original if restore else "WeChat ID: [已遮罩]")
        digest = runner.reception_policy_fingerprint(reception_policy)
        reception_policy.clear()
        seen.append(case.case_id)
        return {"case_id": case.case_id, "status": "passed", "reception_policy_fingerprint": digest}
    cases = [runner.ReplayCase(case_id=str(index), suite="unit", source_case_key="source",
        conversation_id=1, customer_text="Question?", context_messages=[], reference_answer="",
        categories=[], expected_route="peach_9d_2027", expected_action="reply", expected_intent=None,
        expected_group=None, context_mode="historical_route_binding", stage="needs_discovery") for index in range(68)]
    cases[0].customer_text = "WeChat ID: [已遮罩]"
    cases[0].source_case_key = hashlib.sha256(b"1:500").hexdigest()[:24]
    suites = tmp_path / "suites"
    suites.mkdir()
    (suites / "unit.json").write_text(json.dumps([asdict(case) for case in cases]), encoding="utf-8")
    output = tmp_path / "results"
    monkeypatch.setattr(sys, "argv", ["history", "--suites", str(suites), "--output", str(output)]
                        + (["--restore-masked-inputs"] if restore else []))
    monkeypatch.setattr(runner, "SessionLocal", session_factory)
    monkeypatch.setattr(runner, "assert_evaluation_only", lambda: None)
    monkeypatch.setattr(runner, "source_fingerprint", lambda: "source")
    monkeypatch.setattr(runner, "assert_source_unchanged", lambda value: None)
    monkeypatch.setattr(runner, "runtime_config_fingerprint", lambda db: "config")
    monkeypatch.setattr(runner, "candidate_materials", lambda db, tenant: [])
    monkeypatch.setattr(runner, "effective_reception_policy", read_policy)
    monkeypatch.setattr(runner, "run_case", run_case)
    assert runner.main() == 0
    assert len(policy_reads) == 1 and len(seen) == 68
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["reception_policy_fingerprint"] == runner.reception_policy_fingerprint(expected_policy)
    assert report["reception_policy_unchanged"] and report["runtime_config_unchanged"]
    assert report["restored_input_count"] == int(restore)
    assert "private operator tone" not in capsys.readouterr().out
    assert "private operator tone" not in (output / "results.jsonl").read_text(encoding="utf-8")
    assert "synthetic_fixture_only" not in (output / "results.jsonl").read_text(encoding="utf-8")
    assert "synthetic_fixture_only" not in (suites / "unit.json").read_text(encoding="utf-8")
    with session_factory() as db:
        assert effective_reception_policy(db) == expected_policy
