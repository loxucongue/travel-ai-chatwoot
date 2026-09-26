import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analyze_raw_chat_journeys import BATCH_SCHEMA, _conversation_metrics, _require_shape
from analyze_unselected_route_human_flow import (
    OUTPUT_SHAPE,
    conversation_metrics,
    require_shape,
)
from setup_routes_1_2 import _reviewed_metadata_value


def test_route_import_backfills_empty_asset_narratives_without_overwriting_operator_edits():
    assert _reviewed_metadata_value({"caption": ""}, "caption", "reviewed") == "reviewed"
    assert _reviewed_metadata_value({"caption": "operator"}, "caption", "reviewed") == "operator"
    assert _reviewed_metadata_value({"points": []}, "points", ["reviewed"]) == ["reviewed"]
    assert _reviewed_metadata_value({"points": ["operator"]}, "points", ["reviewed"]) == ["operator"]


def test_route_import_migrates_legacy_simplified_copy_only():
    assert _reviewed_metadata_value(
        {"caption": "我先把酒店照片发您看，这里可以看到房间。"},
        "caption",
        "我先把飯店照片發您看～這裡可以看到房間。",
    ) == "我先把飯店照片發您看～這裡可以看到房間。"
    assert _reviewed_metadata_value(
        {"caption": "這是營運另外調整過的繁體說明～"},
        "caption",
        "預設說明",
    ) == "這是營運另外調整過的繁體說明～"


def test_raw_chat_timing_metrics_are_computed_by_code():
    rows = [
        SimpleNamespace(direction="incoming", created_at="2026-09-05T10:00:00+00:00"),
        SimpleNamespace(direction="outgoing", created_at="2026-09-05T10:00:30+00:00"),
        SimpleNamespace(direction="outgoing", created_at="2026-09-05T10:05:30+00:00"),
        SimpleNamespace(direction="incoming", created_at="2026-09-05T10:06:00+00:00"),
    ]
    metrics = _conversation_metrics(rows)
    assert metrics["advisor_response_delay_seconds"] == [30]
    assert metrics["consecutive_outgoing_gaps_minutes"] == [5]
    assert metrics["max_consecutive_outgoing_messages"] == 2
    assert metrics["customer_replied_after_an_outgoing_message"] is True


def test_unselected_metrics_use_non_causal_response_names():
    messages = [
        {"role": "customer", "created_at": "2026-09-05T10:00:00+00:00"},
        {"role": "human_advisor", "created_at": "2026-09-05T10:00:20+00:00"},
        {"role": "customer", "created_at": "2026-09-05T10:01:00+00:00"},
        {"role": "human_advisor", "created_at": "2026-09-05T10:02:00+00:00"},
    ]
    metrics = conversation_metrics(messages)
    assert metrics["advisor_response_delay_seconds"] == [20, 60]
    assert metrics["response_observed_turns"] == 1
    assert metrics["no_response_observed_turns"] == 1


def test_offline_contracts_reject_legacy_worked_failed_shapes():
    legacy = {
        "unselected_route_followups": [],
        "stage_language": {},
        "worked_patterns": [],
        "failed_patterns": [],
        "recommended_unselected_flow": [],
        "evidence": [],
    }
    with pytest.raises(RuntimeError, match="missing_fields"):
        require_shape(legacy, OUTPUT_SHAPE, "unselected")

    missing = {key: value for key, value in BATCH_SCHEMA.items() if key != "evidence"}
    with pytest.raises(RuntimeError, match="missing_fields:evidence"):
        _require_shape(missing, BATCH_SCHEMA, "raw")
