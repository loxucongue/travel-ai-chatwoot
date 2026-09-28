"""Versioned, validated route packages shared by AI and SOP execution."""
from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from collections.abc import MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from functools import lru_cache
from pathlib import Path
from typing import Any



PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "knowledge"
    / "china2go"
    / "route-packages"
)
JOURNEY_POLICY_PATH = PACKAGE_ROOT / "reception-journey-policy.json"
RUNTIME_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "data" / "route-packages"
DEFAULT_INITIAL_DELIVERY_INTERVAL_SECONDS = 2
DELIVERY_MODES = {"text_only", "assets_only", "text_then_assets", "assets_then_text"}


class RoutePackageError(RuntimeError):
    pass






def _require(value: dict, key: str, expected: type, source: Path) -> Any:
    result = value.get(key)
    if not isinstance(result, expected) or (expected in (str, list, dict) and not result):
        raise RoutePackageError(f"route_package_invalid:{source.name}:{key}")
    return result


def _validate(package: dict, source: Path) -> dict:
    if package.get("schema_version") != 1:
        raise RoutePackageError(f"route_package_schema_unsupported:{source.name}")
    route_id = _require(package, "route_variant", str, source)
    _require(package, "package_version", str, source)
    _require(package, "knowledge_version", str, source)
    _require(package, "branch", str, source)
    _require(package, "name", str, source)
    selection_title = _require(package, "selection_title", str, source)
    if len(selection_title) > 20:
        raise RoutePackageError(f"route_package_selection_title_too_long:{route_id}")
    selection_aliases = package.get("selection_aliases", [])
    if (
        not isinstance(selection_aliases, list)
        or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in selection_aliases)
        or len(selection_aliases) != len(set(selection_aliases))
    ):
        raise RoutePackageError(f"route_package_selection_aliases_invalid:{route_id}")
    match_keywords = package.get("match_keywords", [])
    if (
        not isinstance(match_keywords, list)
        or len(match_keywords) > 50
        or any(not isinstance(item, str) or not item.strip() or len(item.strip()) > 40 for item in match_keywords)
    ):
        raise RoutePackageError(f"route_package_match_keywords_invalid:{route_id}")
    normalized_keywords = [item.strip() for item in match_keywords]
    if len({item.casefold() for item in normalized_keywords}) != len(normalized_keywords):
        raise RoutePackageError(f"route_package_match_keywords_duplicate:{route_id}")
    package["match_keywords"] = normalized_keywords
    _require(package, "default_entry_message", str, source)
    ai_guidance = package.get("ai_guidance", "")
    if not isinstance(ai_guidance, str) or len(ai_guidance) > 3000:
        raise RoutePackageError(f"route_package_ai_guidance_invalid:{route_id}")
    initial_delivery_interval = package.get(
        "initial_delivery_interval_seconds", DEFAULT_INITIAL_DELIVERY_INTERVAL_SECONDS
    )
    if (
        not isinstance(initial_delivery_interval, int)
        or isinstance(initial_delivery_interval, bool)
        or not 1 <= initial_delivery_interval <= 30
    ):
        raise RoutePackageError(
            f"route_package_initial_delivery_interval_invalid:{route_id}"
        )
    package["initial_delivery_interval_seconds"] = initial_delivery_interval
    required_slots = _require(package, "required_slots", list, source)
    facts = _require(package, "knowledge_facts", list, source)
    groups = _require(package, "content_groups", dict, source)
    sequence = _require(package, "content_sequence", list, source)


    if len(required_slots) != len(set(required_slots)):
        raise RoutePackageError(f"route_package_duplicate_slots:{route_id}")
    fact_ids: set[str] = set()
    for fact in facts:
        if (not isinstance(fact, dict) or not str(fact.get("id") or "").strip()
                or not str(fact.get("text") or "").strip()
                or not str(fact.get("source_ref") or "").strip()):
            raise RoutePackageError(f"route_package_fact_invalid:{route_id}")
        if fact["id"] in fact_ids:
            raise RoutePackageError(f"route_package_fact_duplicate:{route_id}:{fact['id']}")
        from app.fact_conditions import validate_answer_conditions
        try:
            validate_answer_conditions(fact)
        except ValueError as exc:
            raise RoutePackageError(f"route_package_fact_conditions_invalid:{route_id}:{fact['id']}") from exc
        fact_ids.add(fact["id"])
    if len(sequence) != len(set(sequence)) or any(key not in groups for key in sequence):
        raise RoutePackageError(f"route_package_sequence_invalid:{route_id}")
    allowed_evidence = fact_ids | {"service.requirements", "service.safety"}
    for key, group in groups.items():
        if not isinstance(group, dict) or not str(group.get("approved_text") or "").strip():
            raise RoutePackageError(f"route_package_group_text_missing:{route_id}:{key}")
        for field in ("asset_keys", "evidence_refs"):
            if not isinstance(group.get(field, []), list):
                raise RoutePackageError(f"route_package_group_invalid:{route_id}:{key}:{field}")
        if not isinstance(group.get("initial_delivery", False), bool):
            raise RoutePackageError(f"route_package_group_initial_delivery_invalid:{route_id}:{key}")
        delivery_mode = group.get("delivery_mode", "assets_then_text")
        if delivery_mode not in DELIVERY_MODES:
            raise RoutePackageError(f"route_package_group_delivery_mode_invalid:{route_id}:{key}")
        if delivery_mode == "assets_only" and not group.get("asset_keys"):
            raise RoutePackageError(f"route_package_group_assets_only_empty:{route_id}:{key}")
        group["delivery_mode"] = delivery_mode
        if any(ref not in allowed_evidence for ref in group.get("evidence_refs", [])):
            raise RoutePackageError(f"route_package_group_evidence_invalid:{route_id}:{key}")

    fixed_answers = package.get("fixed_answers", [])
    if not isinstance(fixed_answers, list):
        raise RoutePackageError(f"route_package_fixed_answers_invalid:{route_id}")
    fixed_answer_ids: set[str] = set()
    all_asset_keys = {
        asset for group in groups.values() for asset in group.get("asset_keys", [])
    }
    for answer in fixed_answers:
        answer_id = str(answer.get("id") or "") if isinstance(answer, dict) else ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", answer_id) or answer_id in fixed_answer_ids:
            raise RoutePackageError(f"route_package_fixed_answer_id_invalid:{route_id}:{answer_id}")
        fixed_answer_ids.add(answer_id)
        # Read old published route versions without keeping the old two-switch
        # semantics in the runtime contract.
        if not answer.get("status"):
            if answer.get("enabled") and answer.get("review_state") == "approved":
                answer["status"] = "active"
            elif answer.get("review_state") == "needs_review":
                answer["status"] = "pending_review"
            else:
                answer["status"] = "disabled"
        answer.setdefault(
            "answer_origin",
            "website_verbatim"
            if str(answer.get("source_ref") or "").startswith("website-")
            else "operator_approved",
        )
        answer.pop("enabled", None)
        answer.pop("review_state", None)
        answer.pop("source_text", None)
        for field in ("name", "answer_text", "source_ref", "content_group_key"):
            if not str(answer.get(field) or "").strip():
                raise RoutePackageError(f"route_package_fixed_answer_field_missing:{route_id}:{answer_id}:{field}")
        if answer.get("status") not in {"active", "pending_review", "disabled"}:
            raise RoutePackageError(f"route_package_fixed_answer_status_invalid:{route_id}:{answer_id}")
        if answer.get("answer_origin") not in {"website_verbatim", "operator_approved"}:
            raise RoutePackageError(f"route_package_fixed_answer_origin_invalid:{route_id}:{answer_id}")
        if answer["content_group_key"] not in groups:
            raise RoutePackageError(f"route_package_fixed_answer_group_invalid:{route_id}:{answer_id}")
        for field in ("topics", "fact_ids", "asset_ids", "positive_examples", "negative_examples"):
            if not isinstance(answer.get(field, []), list):
                raise RoutePackageError(f"route_package_fixed_answer_list_invalid:{route_id}:{answer_id}:{field}")
        if not answer.get("topics") or any(not str(item).strip() for item in answer["topics"]):
            raise RoutePackageError(f"route_package_fixed_answer_topics_invalid:{route_id}:{answer_id}")
        party_min = answer.get("party_size_min")
        party_max = answer.get("party_size_max")
        if (
            (party_min is not None and (not isinstance(party_min, int) or party_min < 1))
            or (party_max is not None and (not isinstance(party_max, int) or party_max < 1))
            or (party_min is not None and party_max is not None and party_min > party_max)
        ):
            raise RoutePackageError(f"route_package_fixed_answer_party_size_invalid:{route_id}:{answer_id}")
        if any(ref not in allowed_evidence for ref in answer.get("fact_ids", [])):
            raise RoutePackageError(f"route_package_fixed_answer_evidence_invalid:{route_id}:{answer_id}")
        if any(asset not in all_asset_keys for asset in answer.get("asset_ids", [])):
            raise RoutePackageError(f"route_package_fixed_answer_asset_invalid:{route_id}:{answer_id}")

    for legacy in ('sop', 'runtime_sop', 'journey_policy', 'policies'):
        package.pop(legacy, None)
    return package


@lru_cache(maxsize=1)
def load_route_packages() -> dict[str, dict]:
    if not PACKAGE_ROOT.is_dir():
        raise RoutePackageError(f"route_package_root_missing:{PACKAGE_ROOT}")
    packages: dict[str, dict] = {}
    sources: dict[str, tuple[str, Path]] = {}
    for root in (PACKAGE_ROOT, RUNTIME_PACKAGE_ROOT):
        if not root.is_dir():
            continue
        for source in sorted(root.glob("*/route-package.json")):
            preview = json.loads(source.read_text(encoding="utf-8"))
            route_id = str(preview.get("route_variant") or "")
            if route_id:
                version = str(preview.get("package_version") or "")
                current = sources.get(route_id)
                if current is None or version >= current[0]:
                    sources[route_id] = (version, source)
    for _, source in sources.values():
        package = _validate(json.loads(source.read_text(encoding="utf-8")), source)
        route = package["route_variant"]
        if route in packages:
            raise RoutePackageError(f"route_package_duplicate:{route}")
        package["source_path"] = str(source)
        packages[route] = package
    if not packages:
        raise RoutePackageError("route_packages_empty")
    versions = {item["knowledge_version"] for item in packages.values()}
    if len(versions) != 1:
        raise RoutePackageError("route_package_knowledge_version_mismatch")
    return packages



ROUTE_PACKAGES: dict[str, dict] = {}
ROUTE_BRANCH: dict[str, str] = {}
_ROUTE_VIEW: ContextVar[dict | None] = ContextVar("route_catalog_view", default=None)


class RouteCatalog(MutableMapping):
    """Publish to the backing registry; decision reads use an isolated catalog."""

    def __init__(self):
        self._current = {}
        self._lock = RLock()

    def __getitem__(self, key):
        view = _ROUTE_VIEW.get()
        if view is not None:
            return deepcopy(view[key])
        with self._lock:
            return self._current[key]

    def __iter__(self):
        view = _ROUTE_VIEW.get()
        with self._lock:
            return iter(tuple(view if view is not None else self._current))

    def __len__(self):
        view = _ROUTE_VIEW.get()
        with self._lock:
            return len(view if view is not None else self._current)

    def __setitem__(self, key, value):
        with self._lock:
            self._current[key] = value

    def __delitem__(self, key):
        with self._lock:
            del self._current[key]

    def replace_current(self, routes):
        with self._lock:
            self._current = deepcopy(routes)

    def current_snapshot(self):
        with self._lock:
            return deepcopy(self._current)


ROUTES = RouteCatalog()




ALL_GROUP_KEYS: list[str] = []
_REGISTRY_SIGNATURE: tuple[tuple[str, int, int], ...] = ()


def _package_signature() -> tuple[tuple[str, int, int], ...]:
    rows: list[tuple[str, int, int]] = []
    for root in (PACKAGE_ROOT, RUNTIME_PACKAGE_ROOT):
        if not root.is_dir():
            continue
        for path in root.glob("*/route-package.json"):
            stat = path.stat()
            rows.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(rows))


def reload_route_packages() -> None:
    """Reload package files in place so every importer sees the same registry."""
    global _REGISTRY_SIGNATURE
    load_route_packages.cache_clear()
    packages = load_route_packages()
    compiled = {
        key: {
        "branch": package["branch"],
        "name": package["name"],
        "selection_title": package["selection_title"],
        "selection_aliases": package.get("selection_aliases", []),
        "match_keywords": package.get("match_keywords", []),
        "package_version": package["package_version"],
        "source": deepcopy(package.get("source", {})),
        "default_entry_message": package["default_entry_message"],
        "ai_guidance": package.get("ai_guidance", ""),
        "initial_delivery_interval_seconds": package["initial_delivery_interval_seconds"],
        "required_slots": package["required_slots"],
        "knowledge_facts": package["knowledge_facts"],
        "introduction_sequence": list(package["content_sequence"]),
        "sequence": [
            group_key for group_key in package["content_sequence"]
            if not package["content_groups"][group_key].get("initial_only", False)
        ],
        "groups": {
            group_key: {
                "text": group["approved_text"],
                "assets": group.get("asset_keys", []),
                "evidence": group.get("evidence_refs", []),
                "purpose": group.get("purpose", ""),
                "initial_delivery": bool(group.get("initial_delivery", False)),
                "initial_only": bool(group.get("initial_only", False)),
                "delivery_mode": group.get("delivery_mode", "assets_then_text"),
            }
            for group_key, group in package["content_groups"].items()
        },
        "fixed_answers": deepcopy(package.get("fixed_answers", [])),
        }
        for key, package in packages.items()
    }
    ROUTE_PACKAGES.clear()
    ROUTE_PACKAGES.update(packages)
    ROUTE_BRANCH.clear()
    ROUTE_BRANCH.update({key: value["branch"] for key, value in packages.items()})
    ROUTES.replace_current(compiled)
    ALL_GROUP_KEYS[:] = sorted({key for route in ROUTES.values() for key in route["groups"]})
    _REGISTRY_SIGNATURE = _package_signature()


def ensure_route_packages_current() -> None:
    """Refresh long-running API/worker processes after an operator publishes a route."""
    if _package_signature() != _REGISTRY_SIGNATURE:
        reload_route_packages()


def install_route_package(package: dict) -> Path:
    """Atomically persist a validated operator package and activate the registry."""
    candidate = deepcopy(package)
    candidate.pop("source_path", None)
    candidate.pop("runtime_sop", None)
    checked = _validate(candidate, Path("uploaded-route-package.json"))
    route_id = checked["route_variant"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", route_id):
        raise RoutePackageError(f"route_package_id_invalid:{route_id}")
    RUNTIME_PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    destination = RUNTIME_PACKAGE_ROOT / route_id / "route-package.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    previous = destination.read_bytes() if destination.is_file() else None
    temporary.write_text(json.dumps(checked, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    try:
        reload_route_packages()
    except Exception:
        if previous is None:
            destination.unlink(missing_ok=True)
        else:
            destination.write_bytes(previous)
        reload_route_packages()
        raise
    return destination


def remove_runtime_route_package(route_id: str) -> None:
    """Remove only an operator override; packaged source routes remain available."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", route_id):
        raise RoutePackageError(f"route_package_id_invalid:{route_id}")
    destination = RUNTIME_PACKAGE_ROOT / route_id / "route-package.json"
    destination.unlink(missing_ok=True)
    try:
        destination.parent.rmdir()
    except OSError:
        pass
    reload_route_packages()


reload_route_packages()
KNOWLEDGE_VERSION = next(iter(ROUTE_PACKAGES.values()))["knowledge_version"]


# A customer may stop replying before choosing a supported route.  That state
# must be handled gently: the first reply has already asked the customer to
# choose, so the unclassified journey gets one low-pressure nudge and then
# stops.  The full multi-touch sequence is reserved for a confirmed route.
UNCLASSIFIED_SOP_NAME = "未选线路 · 默认未回复SOP"
UNCLASSIFIED_SOP_DESCRIPTION = (
    "客户尚未选择具体线路时的沉默旅程：只做一次轻量补充，避免重复催选；"
    "客户回复后立即停止，由模型切换到对应线路旅程。"
)
_UNCLASSIFIED_TOUCHES = [
    "我先幫您把差別說簡單：9日不走珠峰，11日會到珠峰大本營。您這次想安排珠峰嗎？",
]
UNCLASSIFIED_SOP_NODES = [
    {
        "key": "route_selection_silence" if index == 0 else f"route_selection_wakeup_{index}",
        "content_group_key": f"route_selection_touch_{index + 1}",
        "schedule_type": "relative",
        "basis": "enrollment" if index == 0 else "previous_node",
        "delay_minutes": delay,
        "journey_trigger": "silence_mainline" if index == 0 else "wakeup",
        "messages": [{
            "key": "text",
            "content_type": "text",
            "content": content,
        }],
        "content_group_candidates": [],
    }
    for index, (delay, content) in enumerate(zip((30,), _UNCLASSIFIED_TOUCHES))
]


def route_package_summary() -> list[dict]:
    ensure_route_packages_current()
    return [
        {
            "route_variant": key,
            "branch": package["branch"],
            "name": package["name"],
            "selection_title": package["selection_title"],
            "match_keywords": package.get("match_keywords", []),
            "package_version": package["package_version"],
            "knowledge_version": package["knowledge_version"],
            "default_entry_message": package["default_entry_message"],
            "required_slots": package["required_slots"],
            "content_groups": len(package["content_groups"]),
            "sop_nodes": len(package["content_sequence"]),
        }
        for key, package in sorted(ROUTE_PACKAGES.items(), key=lambda item: _natural_key(item[0]))
    ]


def _natural_key(value: str) -> list[object]:
    """Keep operator-facing route lists stable (9 before 11) without hardcoding routes."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def route_quick_reply_titles() -> list[str]:
    ensure_route_packages_current()
    return [
        ROUTES[route_id]["selection_title"]
        for route_id in sorted(ROUTES, key=_natural_key)
    ]
