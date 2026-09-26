from copy import deepcopy
from dataclasses import dataclass
import json
import re
import sys

import pytest
from sqlalchemy import event, select

from app.models import AppSetting
from app.release_provenance import runtime_config_fingerprint
from app.route_packages import ROUTES


def test_config_fingerprint_is_canonical_and_opaque(session_factory):
    with session_factory() as db:
        row = AppSetting(key="private-setting", value={"token": "do-not-print", "nested": {"b": 2, "a": 1}})
        db.add(row)
        db.commit()
        first = runtime_config_fingerprint(db)
        row.value = {"nested": {"a": 1, "b": 2}, "token": "do-not-print"}
        db.commit()
        assert runtime_config_fingerprint(db) == first
        assert re.fullmatch(r"[0-9a-f]{64}", first)
        assert "do-not-print" not in first and "private-setting" not in first
        row.value = {"token": "changed"}
        db.commit()
        assert runtime_config_fingerprint(db) != first


def test_config_fingerprint_detects_addition_and_deletion(session_factory):
    with session_factory() as db:
        initial = runtime_config_fingerprint(db)
        row = AppSetting(key="audit-test", value={"enabled": True})
        db.add(row)
        db.commit()
        assert runtime_config_fingerprint(db) != initial
        db.delete(row)
        db.commit()
        assert runtime_config_fingerprint(db) == initial


def test_config_fingerprint_does_not_flush_pending_changes(session_factory):
    with session_factory() as db:
        row = AppSetting(key="audit-test", value={"enabled": True})
        db.add(row)
        db.commit()
        initial = runtime_config_fingerprint(db)
        row.value = {"enabled": False}
        pending = AppSetting(key="pending", value={"secret": "not-persisted"})
        db.add(pending)
        statements = []
        connection = db.connection()
        listener = lambda conn, cursor, statement, parameters, context, many: statements.append(statement)
        event.listen(connection, "before_cursor_execute", listener)
        try:
            assert runtime_config_fingerprint(db) == initial
        finally:
            event.remove(connection, "before_cursor_execute", listener)
        assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        assert row in db.dirty and pending in db.new
        db.rollback()
        assert db.scalar(select(AppSetting.value).where(AppSetting.key == "audit-test")) == {"enabled": True}


def test_config_fingerprint_detects_loaded_route_change(session_factory, monkeypatch):
    with session_factory() as db:
        initial = runtime_config_fingerprint(db)
        route = next(iter(ROUTES))
        changed = deepcopy(ROUTES[route])
        changed["audit_revision"] = "different-loaded-registry"
        monkeypatch.setitem(ROUTES, route, changed)
        assert runtime_config_fingerprint(db) != initial


@pytest.mark.parametrize("change_config,workers", [(False, 1), (True, 1), (False, 3)])
def test_history_report_records_config_and_preserves_business_verdict(
    session_factory, monkeypatch, tmp_path, change_config, workers,
):
    from scripts import run_delivery_history_acceptance as runner

    @dataclass
    class Case:
        case_id: str

    suites = tmp_path / "suites"
    suites.mkdir()
    (suites / "cases.json").write_text(json.dumps([{"case_id": str(i)} for i in range(68)]), encoding="utf-8")
    output = tmp_path / "output"
    argv = ["history", "--suites", str(suites), "--output", str(output)]
    if workers != 1:
        argv.extend(["--workers", str(workers)])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(runner, "ReplayCase", Case)
    monkeypatch.setattr(runner, "SessionLocal", session_factory)
    monkeypatch.setattr(runner, "assert_evaluation_only", lambda: None)
    monkeypatch.setattr(runner, "source_fingerprint", lambda: "source-test")
    monkeypatch.setattr(runner, "assert_source_unchanged", lambda expected: None)
    monkeypatch.setattr(runner, "global_message_sending_enabled", lambda db: False)
    monkeypatch.setattr(runner, "candidate_materials", lambda *args: [])
    monkeypatch.setattr(runner, "outbound_count", lambda db: 0)

    def run_case(case, materials, *, reception_policy=None):
        if change_config and case.case_id in {"1", "2"}:
            with session_factory() as db:
                if case.case_id == "1":
                    db.add(AppSetting(key="temporary-audit-change", value={"secret": "never-in-report"}))
                else:
                    db.delete(db.get(AppSetting, "temporary-audit-change"))
                db.commit()
        return {"case_id": case.case_id, "status": "passed",
                "reception_policy_fingerprint": runner.reception_policy_fingerprint(reception_policy)}

    monkeypatch.setattr(runner, "run_case", run_case)
    assert runner.main() == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["runtime_config_unchanged"] is (not change_config)
    assert report["runtime_config_fingerprint_start"] == report["runtime_config_fingerprint_end"]
    raw = (output / "results.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 68 and report["passed"] == 68
    assert {row["case_id"] for row in rows} == {str(i) for i in range(68)}
    assert report["workers"] == workers
    assert all(row["release_provenance"]["source_fingerprint"] == "source-test" for row in rows)
    assert rows[1]["release_provenance"]["runtime_config_unchanged"] is (not change_config)
    assert "never-in-report" not in raw and "temporary-audit-change" not in raw
