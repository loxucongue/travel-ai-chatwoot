"""Reviewed, cross-route service knowledge captured from China2Go pages."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path


ROOT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "knowledge"
    / "china2go"
    / "service-knowledge"
)
MANIFEST_PATH = ROOT / "service-knowledge.json"


class ServiceKnowledgeError(RuntimeError):
    pass


def _nonempty(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ServiceKnowledgeError(f"service_knowledge_field_missing:{field}")
    return result


def _validate(data: dict) -> dict:
    if data.get("schema_version") != 1:
        raise ServiceKnowledgeError("service_knowledge_schema_unsupported")
    _nonempty(data.get("knowledge_version"), "knowledge_version")
    sources = data.get("sources")
    facts = data.get("facts")
    answers = data.get("fixed_answers")
    if not isinstance(sources, list) or not isinstance(facts, list) or not isinstance(answers, list):
        raise ServiceKnowledgeError("service_knowledge_collections_invalid")

    source_ids: set[str] = set()
    for source in sources:
        source_id = _nonempty(source.get("id") if isinstance(source, dict) else None, "source.id")
        if source_id in source_ids:
            raise ServiceKnowledgeError(f"service_knowledge_source_duplicate:{source_id}")
        source_ids.add(source_id)
        url = _nonempty(source.get("url"), f"source.{source_id}.url")
        if not re.fullmatch(r"https://china2go\.com/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+/?", url):
            raise ServiceKnowledgeError(f"service_knowledge_source_not_allowed:{source_id}")
        relative = Path(_nonempty(source.get("snapshot_path"), f"source.{source_id}.snapshot_path"))
        snapshot = (ROOT / relative).resolve()
        if not snapshot.is_relative_to(ROOT.resolve()) or not snapshot.is_file():
            raise ServiceKnowledgeError(f"service_knowledge_snapshot_missing:{source_id}")
        expected_hash = _nonempty(source.get("snapshot_sha256"), f"source.{source_id}.snapshot_sha256")
        actual_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if expected_hash != actual_hash:
            raise ServiceKnowledgeError(f"service_knowledge_snapshot_hash_mismatch:{source_id}")

    fact_ids: set[str] = set()
    for fact in facts:
        fact_id = _nonempty(fact.get("id") if isinstance(fact, dict) else None, "fact.id")
        if fact_id in fact_ids:
            raise ServiceKnowledgeError(f"service_knowledge_fact_duplicate:{fact_id}")
        fact_ids.add(fact_id)
        _nonempty(fact.get("text"), f"fact.{fact_id}.text")
        source_ref = _nonempty(fact.get("source_ref"), f"fact.{fact_id}.source_ref")
        if source_ref.split("#", 1)[0] not in source_ids:
            raise ServiceKnowledgeError(f"service_knowledge_fact_source_invalid:{fact_id}")

    answer_ids: set[str] = set()
    for answer in answers:
        answer_id = _nonempty(answer.get("id") if isinstance(answer, dict) else None, "answer.id")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", answer_id) or answer_id in answer_ids:
            raise ServiceKnowledgeError(f"service_knowledge_answer_id_invalid:{answer_id}")
        answer_ids.add(answer_id)
        if answer.get("status") not in {"active", "pending_review", "disabled"}:
            raise ServiceKnowledgeError(f"service_knowledge_answer_status_invalid:{answer_id}")
        for field in ("name", "answer_text", "source_ref"):
            _nonempty(answer.get(field), f"answer.{answer_id}.{field}")
        for field in ("topics", "fact_ids", "positive_examples", "negative_examples", "block_signals"):
            if not isinstance(answer.get(field), list):
                raise ServiceKnowledgeError(f"service_knowledge_answer_list_invalid:{answer_id}:{field}")
        if answer.get("status") == "active" and not answer.get("positive_examples"):
            raise ServiceKnowledgeError(f"service_knowledge_answer_examples_missing:{answer_id}")
        if any(str(fact_id) not in fact_ids for fact_id in answer.get("fact_ids") or []):
            raise ServiceKnowledgeError(f"service_knowledge_answer_fact_invalid:{answer_id}")
        if str(answer.get("source_ref") or "").split("#", 1)[0] not in source_ids:
            raise ServiceKnowledgeError(f"service_knowledge_answer_source_invalid:{answer_id}")
    return data


@lru_cache(maxsize=1)
def load_service_knowledge() -> dict:
    if not MANIFEST_PATH.is_file():
        raise ServiceKnowledgeError(f"service_knowledge_manifest_missing:{MANIFEST_PATH}")
    return _validate(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))


SERVICE_KNOWLEDGE = load_service_knowledge()
SERVICE_KNOWLEDGE_VERSION = SERVICE_KNOWLEDGE["knowledge_version"]
SERVICE_FACTS = SERVICE_KNOWLEDGE["facts"]
SERVICE_FIXED_ANSWERS = SERVICE_KNOWLEDGE["fixed_answers"]

