"""Import the reviewed inventory for local rehearsal only; never call Chatwoot."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.db import SessionLocal
from app.models import KnowledgeVersion, MaterialAsset, StoredMedia, Tenant, User, InboxBinding, SopDefinition
from app.automation_service import sop_snapshot
from app.material_library import CATALOG_VERSION
from sqlalchemy import select


def backup():
    if not settings.database_url.startswith("sqlite:///"):
        raise RuntimeError("sqlite_required")
    source = Path(settings.database_url.removeprefix("sqlite:///"))
    folder = source.parent / "backups"
    folder.mkdir(exist_ok=True)
    target = folder / f"before-material-pilot-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    print(json.dumps({"backup": str(target.resolve())}))


def seed():
    if settings.app_profile != "evaluation" or settings.outbound_mode != "disabled" or settings.chatwoot_write_enabled:
        raise RuntimeError("evaluation_only")
    manifest = json.loads((Path(__file__).resolve().parents[2] / "output/materials-2026-08-26/manifest.json").read_text(encoding="utf-8"))
    root = Path(settings.business_materials_dir).resolve()
    approved_for_rehearsal = {f"M{i:02}" for i in range(1, 12)} | {"M24"}
    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant))
        admin = db.scalar(select(User).where(User.role.in_(["super_admin", "admin"]), User.active.is_(True)).order_by(User.id))
        if not tenant or not admin:
            raise RuntimeError("administrator_required")
        version = db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.version_key == CATALOG_VERSION))
        if not version:
            version = KnowledgeVersion(tenant_id=tenant.id, version_key=CATALOG_VERSION, title="旅游共享图片 · 本地演练",
                source_summary={"inventory": "materials-2026-08-26", "files": 105, "unique_images": 44, "live_approved": False},
                content_hash=hashlib.sha256(json.dumps(manifest["assets"],sort_keys=True,ensure_ascii=False).encode()).hexdigest())
            db.add(version)
            db.flush()
        lookup = {}
        for item in manifest["assets"]:
            asset = db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id == version.id, MaterialAsset.asset_key == item["asset_id"]))
            if not asset:
                source = Path(item["representative_path"]).resolve()
                if not source.is_relative_to(root) or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
                    raise RuntimeError("source_changed")
                destination = Path(settings.upload_dir).resolve() / "materials" / f"{item['sha256']}{source.suffix.lower()}"
                destination.parent.mkdir(parents=True,exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(source,destination)
                if hashlib.sha256(destination.read_bytes()).hexdigest()!=item["sha256"]:
                    raise RuntimeError("managed_file_changed")
                media = StoredMedia(tenant_id=tenant.id,original_name=item["title"],media_type="image",
                    mime_type="image/png" if source.suffix.lower()==".png" else "image/jpeg",file_size=destination.stat().st_size,
                    storage_path=str(destination),created_by=admin.id)
                db.add(media)
                db.flush()
                routes = []
                if "G01" in item["group_ids"]:routes.append("peach_11d_2027")
                if "G03" in item["group_ids"]:routes.append("peach_9d_2027")
                asset = MaterialAsset(knowledge_version_id=version.id,asset_key=item["asset_id"],source_path=str(destination),
                    display_name=item["title"],media_type="image",usage=item["topic"],file_hash=item["sha256"],available=True,
                    metadata_json={"stored_media_id":media.id,"display_id":item["display_id"],"content_family":item["content_family"],
                        "original_paths":item["aliases"],"route_variants":routes,"review_notes":item["review_notes"],
                        "review_state":"evaluation_ready" if item["display_id"] in approved_for_rehearsal else "pending_review",
                        "live_approved":False})
                db.add(asset)
                db.flush()
            if not asset.metadata_json.get("content_group_key"):
                group = "hotel_room_intro" if item["display_id"] in ("M03", "M08") else item["content_family"]
                asset.metadata_json = {**asset.metadata_json, "content_group_key":group}
            lookup[item["display_id"]]=asset
        inbox = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id==tenant.id,InboxBinding.chatwoot_inbox_id==128859))
        if not inbox:raise RuntimeError("facebook_inbox_not_synced")
        strategy_ids=[]
        for days, poster in [(11,"M04"),(9,"M24")]:
            name=f"桃花{days}日 · 图文演练（测试会话26）"
            sop=db.scalar(select(SopDefinition).where(SopDefinition.tenant_id==tenant.id,SopDefinition.name==name))
            if not sop:
                route=f"peach_{days}d_2027"
                introductions=[
                    ("行程总览",10,"enrollment",poster,f"這是2027林芝桃花{'＋珠峰' if days==11 else ''}{days}日的行程參考，實際出發安排請由顧問確認。"),
                    ("住宿参考",20,"enrollment","M08","提供路線資料中的客房參考，實際入住酒店與房型需由顧問確認。"),
                    ("车辆参考",10,"previous_node","M09","提供車內環境參考，實際用車安排需由顧問確認。")]
                nodes=[]
                for key,delay,basis,identifier,text in introductions:
                    asset=lookup[identifier]
                    nodes.append({"key":key,"schedule_type":"relative","basis":basis,"delay_minutes":delay,"skip_if_materials_provided":True,
                        "messages":[{"key":"intro","content_type":"text","content":text},
                                    {"key":"photo","content_type":"image","content":"","media_id":asset.metadata_json["stored_media_id"],
                                     "asset_key":asset.asset_key,"media_name":asset.display_name}]})
                sop=SopDefinition(tenant_id=tenant.id,name=name,description="本地演练；手工首次入组；仅测试会话26或隔离模拟客户；客户回复立即结束本轮。",
                    status="running",version=1,route_variant=route,test_conversation_ids=[26],dry_run=True,live_enabled=False,
                    trigger_type="manual",trigger_labels=[],inbox_ids=[128859],nodes=nodes,exit_labels=[],stop_on_incoming=True,
                    frequency_hours=24,created_by=admin.id)
                db.add(sop)
                db.flush()
                sop_snapshot(db,sop,admin.id)
            elif sop.version == 1 and sop.test_conversation_ids == [26] and not any(n.get("skip_if_materials_provided") for n in sop.nodes):
                sop.nodes=[{**n,"skip_if_materials_provided":True} for n in sop.nodes]
                sop.version += 1
                sop_snapshot(db,sop,admin.id)
            strategy_ids.append(sop.id)
        db.commit()
        print(json.dumps({"catalog_assets":len(lookup),"evaluation_ready":sum(x.metadata_json["review_state"]=="evaluation_ready" for x in lookup.values()),
            "sop_ids":strategy_ids,"test_chatwoot_conversation_ids":[26],"outbound":False}))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser()
    parser.add_argument("--backup-only",action="store_true")
    args=parser.parse_args()
    if args.backup_only:backup()
    else:seed()
