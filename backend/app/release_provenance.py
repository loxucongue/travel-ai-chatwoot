"""Read-only identity for the source and route files exercised by acceptance."""
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from app.models import AppSetting
from app.route_packages import ROUTES


def runtime_config_fingerprint(db) -> str:
    """Hash persisted settings and the loaded registry without writes or secrets in output.

    This is a point-in-time observation, not a lock or a change-history proof.
    Pending ORM edits are deliberately neither flushed nor fingerprinted.
    """
    with db.no_autoflush:
        settings = dict(db.execute(select(AppSetting.key, AppSetting.value)).all())
    canonical = json.dumps(
        {"schema_version": 1, "app_settings": settings, "route_registry": ROUTES.current_snapshot()},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_fingerprint() -> str:
    root = Path(__file__).resolve().parents[2]
    inputs = []
    for folder, pattern in (("backend/app", "*.py"), ("backend/alembic", "*.py"), ("backend/app/reception_v2/skills", "SKILL.md"),
                            ("backend/scripts", "*.py"), ("frontend/src", "*"),
                            ("data/knowledge/china2go/route-packages", "*.json"),
                            ("data/knowledge/china2go/service-knowledge", "*.json"),
                            ("data/knowledge/china2go/global-website-knowledge", "*.json")):
        inputs.extend(path for path in (root / folder).rglob(pattern) if path.is_file())
    inputs.extend(root/name for name in ('backend/pyproject.toml','frontend/package.json','frontend/package-lock.json')
                  if (root/name).is_file())
    digest = hashlib.sha256()
    for path in sorted(inputs):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def assert_source_unchanged(expected: str) -> None:
    if source_fingerprint() != expected:
        raise RuntimeError("acceptance_source_changed_during_run")
