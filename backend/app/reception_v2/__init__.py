"""Skill-driven V2 customer reception engine."""

from app.reception_v2.skill_registry import SkillRegistry
import hashlib
from pathlib import Path

ENGINE_VERSION = "v2"
SKILL_RELEASE_DIGEST = SkillRegistry().release_digest()


def _release_digest() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256(SKILL_RELEASE_DIGEST.encode())
    files = [*root.glob("*.py"), *[root.parent / name for name in (
        "decision_knowledge.py", "decision_service.py", "reply_fact_verification.py",
        "reception_config.py", "live_sop.py", "automation_service.py",
        "deepseek_evaluation.py", "model_gateway.py", "route_reply.py", "route_packages.py",
        "delivery_tracking.py", "delivery_plan.py", "live_reply.py", "material_library.py",
        "reception_policy_views.py", "advisor_voice.py", "customer_contact_policy.py",
        "automation_api.py", "lead_capture.py", "operations.py", "reply_generation.py",
        "web_knowledge.py", "service_knowledge.py", "fact_conditions.py",
        "contact_channels.py", "reply_failures.py", "turn_context.py", "model_metering.py",
    )]]
    for path in sorted(files):
        digest.update(path.relative_to(root.parent).as_posix().encode())
        digest.update(path.read_bytes())
    knowledge = root.parents[2] / 'data' / 'knowledge' / 'china2go'
    for directory in ('route-packages', 'service-knowledge', 'global-website-knowledge'):
        for path in sorted((knowledge / directory).rglob('*.json')):
            digest.update(path.relative_to(knowledge).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


ENGINE_RELEASE_ID = f"reception-v2-agent-20260927-{_release_digest()[:16]}"
