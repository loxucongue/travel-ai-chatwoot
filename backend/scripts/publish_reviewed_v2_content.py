"""Publish the reviewed global knowledge and altitude guide without sending messages."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import SessionLocal
from app.material_library import material_live_approved, replace_asset_binding
from app.models import AuditLog, KnowledgeVersion, MaterialAsset, StoredMedia, Tenant, User, utcnow
from app.route_packages import ROUTES
from app.route_reply import KNOWLEDGE_VERSION
from app.web_knowledge import install_curated_global_library, publish_revision


ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "data/knowledge/china2go/global-website-knowledge/curated-modules.json"
ASSET_KEY = "china2go-altitude-guide-v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sqlite_backup(target: Path) -> None:
    if not settings.database_url.startswith("sqlite:///"):
        return
    source_path = Path(settings.database_url.removeprefix("sqlite:///"))
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    with sqlite3.connect(target) as check:
        assert check.execute("pragma integrity_check").fetchone()[0] == "ok"


def current_state() -> dict:
    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant).order_by(Tenant.id))
        asset = db.scalar(select(MaterialAsset).where(MaterialAsset.asset_key == ASSET_KEY).order_by(MaterialAsset.id))
        from app.models import WebKnowledgeSource, WebKnowledgeRevision
        source = db.scalar(select(WebKnowledgeSource).where(
            WebKnowledgeSource.tenant_id == tenant.id,
            WebKnowledgeSource.name == "China2Go 官網全局知識",
        ).order_by(WebKnowledgeSource.id)) if tenant else None
        revision = db.get(WebKnowledgeRevision, source.published_revision_id) if source and source.published_revision_id else None
        return {
            "tenant_id": tenant.id if tenant else None,
            "web": {
                "source_id": source.id if source else None,
                "published_revision_id": source.published_revision_id if source else None,
                "runtime_scope": source.runtime_scope if source else None,
                "content_hash": revision.content_hash if revision else None,
            },
            "asset": {
                "id": asset.id if asset else None,
                "file_hash": asset.file_hash if asset else None,
                "available": asset.available if asset else False,
                "live_approved": bool(asset and (asset.metadata_json or {}).get("live_approved") is True),
            },
        }


def publish(pdf_source: Path, storage_path: Path, backup_path: Path | None) -> dict:
    if not pdf_source.is_file() or pdf_source.suffix.lower() != ".pdf":
        raise RuntimeError("reviewed_pdf_missing")
    payload = json.loads(CURATED.read_text(encoding="utf8"))
    digest = sha256(pdf_source)
    if backup_path:
        sqlite_backup(backup_path)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_source.resolve() != storage_path.resolve():
        temporary = storage_path.with_suffix(storage_path.suffix + ".tmp")
        shutil.copy2(pdf_source, temporary)
        temporary.replace(storage_path)
    if sha256(storage_path) != digest:
        raise RuntimeError("reviewed_pdf_copy_mismatch")

    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant).order_by(Tenant.id))
        user = db.scalar(select(User).where(User.active.is_(True)).order_by(User.id))
        if not tenant or not user:
            raise RuntimeError("publication_identity_missing")

        source, revision = install_curated_global_library(
            db, payload, tenant_id=tenant.id, user_id=user.id,
        )
        publish_revision(db, source, revision, runtime_scope="live")
        db.add(AuditLog(
            tenant_id=tenant.id, user_id=user.id, action="web_knowledge.publish",
            resource_type="web_knowledge_revision", resource_id=str(revision.id),
            details={"content_hash": revision.content_hash, "runtime_scope": "live", "review": "user_authorized_final_publication"},
        ))

        version_key = KNOWLEDGE_VERSION
        version = db.scalar(select(KnowledgeVersion).where(
            KnowledgeVersion.tenant_id == tenant.id,
            KnowledgeVersion.version_key == version_key,
        ))
        if not version:
            version = KnowledgeVersion(
                tenant_id=tenant.id, version_key=version_key,
                title="China2Go 桃花9日与11日", content_hash="published-route-packages",
            )
            db.add(version)
            db.flush()
        media = db.scalar(select(StoredMedia).where(
            StoredMedia.tenant_id == tenant.id,
            StoredMedia.storage_path == str(storage_path),
        ))
        if not media:
            media = StoredMedia(
                tenant_id=tenant.id, original_name=storage_path.name,
                media_type="file", mime_type="application/pdf",
                file_size=storage_path.stat().st_size, storage_path=str(storage_path),
                created_by=user.id,
            )
            db.add(media)
            db.flush()
        else:
            media.file_size = storage_path.stat().st_size
            media.mime_type = "application/pdf"
            media.media_type = "file"

        asset = db.scalar(select(MaterialAsset).where(
            MaterialAsset.knowledge_version_id == version.id,
            MaterialAsset.asset_key == ASSET_KEY,
        ))
        if not asset:
            asset = MaterialAsset(
                knowledge_version_id=version.id, asset_key=ASSET_KEY,
                source_path="", display_name="西藏行前：高原與健康準備",
                media_type="file", usage="客戶索取高原行前資料時交付",
                available=False,
                metadata_json={
                    "route_variants": ["peach_9d_2027", "peach_11d_2027"],
                    "content_group_key": "altitude_guide",
                    "content_family": ASSET_KEY,
                    "review_state": "pending", "live_approved": False,
                },
            )
            db.add(asset)
            db.flush()
        before = deepcopy(asset.metadata_json or {})
        replace_asset_binding(db, asset, media, metadata_updates={
            "route_variants": ["peach_9d_2027", "peach_11d_2027"],
            "content_group_key": "altitude_guide",
            "content_family": ASSET_KEY,
            "what_it_shows": "西藏高原行前准备、两条桃花线路差异、供氧、用药与途中不适处理",
            "feature_points": ["9日与11日线路差异", "供氧使用", "用药准备", "就医协助"],
            "customer_value": "出发前可直接照单准备，也方便把行程信息带给医疗专业人员",
            "recommended_caption": "這份把兩條行程的海拔差異、供氧安排、用藥準備和途中不舒服時的處理方式整理在一起。",
            "avoid_claims": ["保证不会高反", "提供个人用药剂量", "导游替代医生"],
        })
        approved = dict(asset.metadata_json or {})
        approved.update({
            "review_state": "evaluation_ready", "live_approved": True,
            "reviewed_by": user.id, "reviewed_at": utcnow(),
            "review_notes": "内容与版面已复核；客户导向、无待审水印，按最终发布授权上线。",
            "approved_file_hash": digest,
        })
        asset.metadata_json = approved
        asset.display_name = "西藏行前：高原與健康準備"
        asset.usage = "客戶索取高原行前資料時交付"
        db.add(AuditLog(
            tenant_id=tenant.id, user_id=user.id, action="route_asset.approved",
            resource_type="asset", resource_id=str(asset.id),
            details={"asset_key": ASSET_KEY, "hash": digest, "previous": before, "review": "user_authorized_final_publication"},
        ))
        db.flush()
        if not material_live_approved(db, asset, media, expected_hash=digest):
            raise RuntimeError("approved_asset_runtime_check_failed")
        db.commit()
        return {
            "tenant_id": tenant.id,
            "web_source_id": source.id,
            "web_revision_id": revision.id,
            "web_revision_number": revision.revision_number,
            "web_hash": revision.content_hash,
            "web_runtime_scope": source.runtime_scope,
            "asset_id": asset.id,
            "media_id": media.id,
            "asset_key": ASSET_KEY,
            "asset_hash": digest,
            "asset_path": str(storage_path),
            "asset_live_approved": True,
            "outbound": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--storage-path", type=Path, required=True)
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = publish(args.pdf, args.storage_path, args.backup_path) if args.apply else {
        "current": current_state(),
        "candidate_pdf_hash": sha256(args.pdf),
        "candidate_web_hash": hashlib.sha256(json.dumps(
            json.loads(CURATED.read_text(encoding="utf8")), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf8")).hexdigest(),
        "outbound": False,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
