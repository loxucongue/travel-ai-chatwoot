"""Import the reviewed first two website routes into local managed storage."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import SessionLocal
from app.automation_service import sop_snapshot
from app.models import (KnowledgeVersion, MaterialAsset, RouteBranch, RouteNode, SopDefinition,
                        StoredMedia, Tenant, User)
from app.route_packages import (KNOWLEDGE_VERSION, ROUTE_BRANCH, ROUTE_PACKAGES, ROUTES,
                                UNCLASSIFIED_SOP_DESCRIPTION, UNCLASSIFIED_SOP_NAME,
                                UNCLASSIFIED_SOP_NODES)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "data/knowledge/china2go/routes-1-2/manifest.json"
ASSET_ROOT = MANIFEST_PATH.parent
ASSET_NARRATIVE_VERSION = "2026-09-08.taiwan-voice-1"

ASSETS = {
    "0ba0fb527a4d804c70d8d9ec06df2af0195d8131157dac2ebeba76bbe94ceb83": ("routes12-11d-itinerary", "11日行程圖", "itinerary_overview", "route_overview:11d", ["peach_11d_2027"]),
    "92bc2dabc7bfe54839b2519322294d59614609907a84eb21eb6f03a25da871d8": ("routes12-9d-itinerary", "9日行程總覽", "itinerary_overview", "route_overview:9d", ["peach_9d_2027"]),
    "647c1d2962263336c00ad477316837cb5abf4f2071b2c7f3ab385ecc0d2e4a4d": ("routes12-rongbuk-room", "絨布旅館", "rongbuk_reference", "hotel:rongbuk", ["peach_11d_2027"]),
    "1d77ee41793b8cf8ef203e7bebe5f4c28d6992d88f78ed05478a0643e869aa0e": ("routes12-pabongka", "帕邦喀寺", "peach_highlights", "attraction:pabongka", ["peach_9d_2027", "peach_11d_2027"]),
    "a1afb18491cc4670770f1ae9bd6f6c4ade040f9f41b6424cdcf91275e7c7ca32": ("routes12-xiuba-fort", "秀巴古堡", "peach_highlights", "attraction:xiuba", ["peach_9d_2027", "peach_11d_2027"]),
    "550f0d06696a9b1289569d3b55f1943797c2d71ca3207f1334d7a61668b3a1bc": ("routes12-hilton-room", "希爾頓客房", "hotel_reference", "hotel:hilton-room", ["peach_9d_2027", "peach_11d_2027"]),
    "3155c76200a2148e6e62ec5731bdf8b730395998ac92eb921e623adc97e9b972": ("routes12-hilton-oxygen", "希爾頓製氧機", "hotel_reference", "equipment:hotel-oxygen", ["peach_9d_2027", "peach_11d_2027"]),
    "847b006731d7f7506eae9a6a13ac7b4e6aa96af4af14145ca64529c6f33e9ec5": ("routes12-vehicle", "車輛照片", "vehicle_reference", "vehicle:interior", ["peach_9d_2027", "peach_11d_2027"]),
    "43a6bd86cba50578561890287fcdb42ac2d00b512359704d5f3d4e33469d2c3f": ("routes12-vehicle-oxygen", "車輛製氧機", "vehicle_reference", "equipment:vehicle-oxygen", ["peach_9d_2027", "peach_11d_2027"]),
    "77a897f4abf8c013e2ea1fcd9a22285e7d2eda6c623443733f41b6488122f7fa": ("routes12-hotel-secondary", "住宿第二張", "accommodation_summary", "hotel:secondary", ["peach_9d_2027", "peach_11d_2027"]),
    "4db76089c0af86f4fe492323a2a0c4dcfc84890f578ecac77ec8c560c978d8f3": ("routes12-potala", "布達拉宮", "landmarks", "attraction:potala", ["peach_9d_2027", "peach_11d_2027"]),
    "9c40203706537c89a8db01586ea3b032678177b370179136d7279dba701c0e67": ("routes12-barkhor", "八廓街", "landmarks", "attraction:barkhor", ["peach_9d_2027", "peach_11d_2027"]),
    "59eb45bb76f1319c9cf88e1410628c92ae1939106e1e41eddc7f737a658b69c1": ("routes12-zhaji", "扎基寺財神廟", "zhaji", "attraction:zhaji", ["peach_9d_2027", "peach_11d_2027"]),
}

ASSET_NARRATIVES = {
    "routes12-11d-itinerary": {
        "what_it_shows": "桃花加珠峰11日每日行程、路線順序和主要停留點",
        "feature_points": ["林芝和波密桃花段", "拉薩、山南和日喀則", "珠峰大本營段"],
        "customer_value": "讓客戶一次看清11天如何串聯桃花、經典景點和珠峰，方便與同行者比較",
        "recommended_caption": "11日完整行程圖放在這裡～每天走哪裡、哪一段會到珠峰都標清楚了，和同行者一起看也很方便。",
        "avoid_claims": ["保證看到珠峰", "即時餘位充足", "這是最輕鬆的路線"],
    },
    "routes12-9d-itinerary": {
        "what_it_shows": "桃花9日每日行程、路線順序和主要停留點",
        "feature_points": ["林芝和波密桃花段", "拉薩、山南和日喀則", "全程不走珠峰"],
        "customer_value": "讓客戶一次看清9天路線範圍，並直接判斷是否符合不去珠峰的偏好",
        "recommended_caption": "9日完整行程圖放在這裡～每天的路線和主要景點都排好了，這條不走珠峰，一眼就能看懂整體走法。",
        "avoid_claims": ["保證看到最佳花期", "即時餘位充足", "任何人都適合"],
    },
    "routes12-rongbuk-room": {
        "what_it_shows": "珠峰段絨布旅館的客房環境",
        "feature_points": ["絨布旅館客房", "獨立衛浴", "房內供氧設備"],
        "customer_value": "讓客戶在決定是否前往珠峰前，先看清楚珠峰段實際住宿配置",
        "recommended_caption": "這張是珠峰段入住的絨布旅館～客房裡有獨立衛浴和供氧設備，珠峰當晚住什麼環境，您可以先看清楚。",
        "avoid_claims": ["保證不會高反", "珠峰最好的住宿", "房間一定有餘位"],
    },
    "routes12-pabongka": {
        "what_it_shows": "帕邦喀寺桃花與寺院環境",
        "feature_points": ["桃花景觀", "寺院人文環境", "路線中的桃花特色點"],
        "customer_value": "讓客戶看到路線不只停留在常規桃花村，也包含寺院人文場景",
        "recommended_caption": "這張是帕邦喀寺的桃花～除了桃花村，沿線也會走進寺院人文和桃花交織的景色。",
        "avoid_claims": ["保證花開程度", "現場一定無人", "最佳拍攝機位"],
    },
    "routes12-xiuba-fort": {
        "what_it_shows": "秀巴古堡與周邊景觀",
        "feature_points": ["千年古堡景觀", "沿線人文地點", "桃花路線中的非賞花內容"],
        "customer_value": "幫助客戶了解桃花路線也包含歷史人文內容，不是連續重複賞花",
        "recommended_caption": "這張是秀巴古堡～桃花之外也會穿插沿線歷史人文，整趟不會只有單一賞花內容。",
        "avoid_claims": ["獨家景點", "保證開放", "可隨意調整停留時間"],
    },
    "routes12-hilton-room": {
        "what_it_shows": "希爾頓客房的實際房間環境",
        "feature_points": ["客房空間", "床鋪與休息區域", "房間內實際陳設"],
        "customer_value": "讓客戶直接查看路線住宿環境，而不是只看到飯店名稱",
        "recommended_caption": "這張是希爾頓客房～房間空間、床鋪和休息區都拍得很清楚，實際住宿環境可以直接看看。",
        "avoid_claims": ["所有房間完全相同", "保證睡得更好", "比其他飯店更安全"],
    },
    "routes12-hilton-oxygen": {
        "what_it_shows": "希爾頓客房內的供氧設備位置與房間環境",
        "feature_points": ["房內供氧設備", "設備安裝位置", "客房實際環境"],
        "customer_value": "讓客戶確認住宿資料中提到的供氧設備有對應實景",
        "recommended_caption": "照片紅框的位置就是客房內的供氧設備～設備裝在哪裡、和房間怎麼配置，都看得很清楚。",
        "avoid_claims": ["保證不會高反", "醫療級治療效果", "保證更安全或更舒適"],
    },
    "routes12-vehicle": {
        "what_it_shows": "9座VIP航空座椅車輛的內部配置",
        "feature_points": ["獨立航空座椅", "座椅排列和車內通道", "小團使用的車輛內部"],
        "customer_value": "讓客戶直接了解多日公路行程實際使用的座椅和車內空間",
        "recommended_caption": "這張是行程使用的9座VIP航空座椅車～座椅排列和車內空間都拍得到，多日行程會搭什麼車一看就清楚。",
        "avoid_claims": ["保證完全不累", "2025年最新車輛", "任何日期車型完全相同"],
    },
    "routes12-vehicle-oxygen": {
        "what_it_shows": "車輛內部安裝的瀰散式供氧設備",
        "feature_points": ["車載供氧設備", "設備在車內的安裝位置", "路線車輛的供氧配置"],
        "customer_value": "讓客戶看到車輛供氧配置的實際設備，而不是只看到文字說明",
        "recommended_caption": "這張是車內的瀰散式供氧設備～照片能看到設備在車廂裡的位置，車輛配置會比較好理解。",
        "avoid_claims": ["移動氧艙", "保證不會高反", "可替代醫療設備"],
    },
    "routes12-hotel-secondary": {
        "what_it_shows": "路線住宿的另一組客房環境",
        "feature_points": ["客房整體環境", "床鋪與活動空間", "可供同行者共同查看的住宿實景"],
        "customer_value": "補充不同角度的住宿環境，方便客戶與同行者一起判斷住宿條件",
        "recommended_caption": "再給您一張不同角度的住宿照片～房間整體和活動空間都看得到，也很方便轉給同行家人一起看。",
        "avoid_claims": ["每晚都是同一房型", "保證房型升級", "所有地區住宿一致"],
    },
    "routes12-potala": {
        "what_it_shows": "布達拉宮景觀",
        "feature_points": ["拉薩經典景點", "路線中的城市人文段", "可與桃花和珠峰內容一起比較"],
        "customer_value": "讓客戶確認路線除了自然景觀，也安排拉薩經典人文景點",
        "recommended_caption": "這張是布達拉宮～除了桃花或珠峰，行程也會走到拉薩的經典人文地標。",
        "avoid_claims": ["保證進入所有區域", "門票即時有餘", "可隨意更改參觀時間"],
    },
    "routes12-barkhor": {
        "what_it_shows": "八廓街街區與拉薩城市人文環境",
        "feature_points": ["八廓街街區", "拉薩城市人文", "路線中的步行遊覽內容"],
        "customer_value": "幫助客戶直觀看到拉薩段不只有地標，也包含城市街區體驗",
        "recommended_caption": "這張是八廓街～拉薩段除了布達拉宮，也會走進老城街區看看當地人文。",
        "avoid_claims": ["保證自由活動時長", "所有商店開放", "沒有人流"],
    },
    "routes12-zhaji": {
        "what_it_shows": "扎基寺及其寺院環境",
        "feature_points": ["扎基寺", "拉薩寺院人文", "路線中的特色人文地點"],
        "customer_value": "讓客戶了解路線包含常規地標之外的寺院人文內容",
        "recommended_caption": "這張是扎基寺～除了布達拉宮和八廓街，行程裡也有更貼近當地生活的寺院人文。",
        "avoid_claims": ["最靈驗", "祈福一定有效", "獨家進入"],
    },
}

SIMPLIFIED_NARRATIVE_CHARACTERS = frozenset(
    "发这们线图间价后现还让从较见过说给对进实车团开关点华转读张当时经资问号满"
)


def _reviewed_metadata_value(current_metadata: dict, key: str, default):
    """Preserve a meaningful operator edit, but backfill legacy empty values."""
    current = current_metadata.get(key)
    if isinstance(default, list):
        if isinstance(current, list) and any(str(item).strip() for item in current):
            joined = "".join(str(item) for item in current)
            return default if any(char in SIMPLIFIED_NARRATIVE_CHARACTERS for char in joined) else current
        return default
    if isinstance(current, str) and current.strip():
        return default if any(char in SIMPLIFIED_NARRATIVE_CHARACTERS for char in current) else current
    return default

LEGACY_GROUPS = {
    "itinerary_peach_9d_2027": "itinerary_overview",
    "itinerary_peach_11d_2027": "itinerary_overview",
    "rongbuk_room": "rongbuk_reference",
    "pabongka_peach": "peach_highlights",
    "xiuba_peach": "peach_highlights",
    "hilton_room_reference": "hotel_reference",
    "hotel_oxygen_room_photo": "hotel_reference",
    "hotel_twin_room_secondary": "accommodation_summary",
    "vip_vehicle_interior": "vehicle_reference",
    "vehicle_oxygen_photo": "vehicle_reference",
    "potala_street_reference": "landmarks",
    "zaki_temple_entrance": "zhaji",
}


def backup_database() -> str | None:
    if not settings.database_url.startswith("sqlite:///"):
        return None
    source = Path(settings.database_url.removeprefix("sqlite:///")).resolve()
    if not source.is_file():
        return None
    destination = ROOT / "data" / "backups" / f"route-packages-{datetime.now():%Y%m%d-%H%M%S}.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as current, sqlite3.connect(destination) as backup:
        current.backup(backup)
    return str(destination)


def _manifest_images(manifest: dict) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for route in manifest["routes"]:
        for image in route["images"]:
            rows.setdefault(image["sha256"], image)
    return rows


def _sop_nodes(package: dict, assets: dict[str, MaterialAsset]) -> list[dict]:
    def bind_messages(source_messages: list[dict]) -> list[dict]:
        messages = []
        for source_message in source_messages:
            message = dict(source_message)
            if message.get("content_type", "text") != "text":
                asset = assets.get(message.get("asset_key"))
                media_id = (asset.metadata_json or {}).get("stored_media_id") if asset else None
                if not asset or not asset.available or not media_id:
                    raise RuntimeError(f"route_asset_unavailable:{message.get('asset_key')}")
                message.update({"media_id": media_id, "media_name": asset.display_name})
            messages.append(message)
        return messages

    nodes = []
    for source_node in package["runtime_sop"]["nodes"]:
        candidates = [{**candidate, "messages": bind_messages(candidate["messages"])}
                      for candidate in source_node.get("content_group_candidates", [])]
        nodes.append({**source_node, "messages": bind_messages(source_node.get("messages", [])),
                      "content_group_candidates": candidates})
    return nodes


def _seed_default_sop(db, tenant: Tenant, admin: User, package: dict,
                      assets: dict[str, MaterialAsset]) -> SopDefinition:
    source = package["runtime_sop"]
    values = {
        "description": source["description"],
        "status": "running",
        "dry_run": True,
        "live_enabled": False,
        "trigger_type": source["trigger_type"],
        "trigger_labels": source["trigger_labels"],
        "inbox_ids": [128859],
        "nodes": _sop_nodes(package, assets),
        "exit_labels": source["exit_labels"],
        "stop_on_incoming": source["stop_on_incoming"],
        "frequency_hours": source["frequency_hours"],
        "route_variant": package["route_variant"],
        "test_conversation_ids": source["test_conversation_ids"],
    }
    sop = db.scalar(select(SopDefinition).where(
        SopDefinition.tenant_id == tenant.id,
        SopDefinition.name == source["name"],
    ))
    if sop is None:
        sop = SopDefinition(
            tenant_id=tenant.id,
            name=source["name"],
            version=1,
            created_by=admin.id,
            **values,
        )
        db.add(sop)
        db.flush()
        sop_snapshot(db, sop, admin.id)
        return sop
    changed = any(getattr(sop, key) != value for key, value in values.items())
    if changed:
        for key, value in values.items():
            setattr(sop, key, value)
        sop.version += 1
        sop_snapshot(db, sop, admin.id)
    return sop


def _seed_unclassified_sop(db, tenant: Tenant, admin: User) -> SopDefinition:
    values = {
        "description": UNCLASSIFIED_SOP_DESCRIPTION,
        "status": "running",
        "dry_run": True,
        "live_enabled": False,
        "trigger_type": "manual",
        "trigger_labels": [],
        "inbox_ids": [128859],
        "nodes": UNCLASSIFIED_SOP_NODES,
        "exit_labels": ["人工接管", "客诉", "拒绝联系", "黑名单", "已留资", "已成交"],
        "stop_on_incoming": True,
        "frequency_hours": 24,
        "route_variant": "",
        "test_conversation_ids": [26],
    }
    sop = db.scalar(select(SopDefinition).where(
        SopDefinition.tenant_id == tenant.id,
        SopDefinition.name == UNCLASSIFIED_SOP_NAME,
    ))
    if sop is None:
        sop = SopDefinition(
            tenant_id=tenant.id,
            name=UNCLASSIFIED_SOP_NAME,
            version=1,
            created_by=admin.id,
            **values,
        )
        db.add(sop)
        db.flush()
        sop_snapshot(db, sop, admin.id)
        return sop
    changed = any(getattr(sop, key) != value for key, value in values.items())
    if changed:
        for key, value in values.items():
            setattr(sop, key, value)
        sop.version += 1
        sop_snapshot(db, sop, admin.id)
    return sop


def run() -> dict:
    backup_path = backup_database()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    images = _manifest_images(manifest)
    if set(images) != set(ASSETS):
        raise RuntimeError("route_manifest_changed")
    package_paths = sorted(Path(package["source_path"]) for package in ROUTE_PACKAGES.values())
    digest = hashlib.sha256(b"".join([
        MANIFEST_PATH.read_bytes(),
        *(path.read_bytes() for path in package_paths),
        json.dumps(ASSET_NARRATIVES, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    ])).hexdigest()
    source_summary = {
        "url": manifest["source_url"],
        "asset_manifest": str(MANIFEST_PATH.relative_to(ROOT)),
        "website_snapshot": "data/knowledge/china2go/website-7693-full/manifest.json",
        "route_packages": [str(path.relative_to(ROOT)) for path in package_paths],
        "routes": sorted(ROUTE_PACKAGES),
        "password_stored": False,
    }
    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant).order_by(Tenant.id))
        admin = db.scalar(select(User).where(
            User.role.in_(["super_admin", "admin"]), User.active.is_(True)
        ).order_by(User.id))
        if not tenant or not admin:
            raise RuntimeError("administrator_required")
        version = db.scalar(select(KnowledgeVersion).where(
            KnowledgeVersion.version_key == KNOWLEDGE_VERSION
        ))
        if version is None:
            version = KnowledgeVersion(
                tenant_id=tenant.id,
                version_key=KNOWLEDGE_VERSION,
                title="China2Go 桃花9日／桃花珠峰11日",
                source_summary=source_summary,
                content_hash=digest,
                status="active",
            )
            db.add(version)
            db.flush()
        else:
            version.content_hash = digest
            version.source_summary = source_summary
            version.status = "active"

        destination_root = Path(settings.upload_dir).resolve() / "materials" / "routes-1-2"
        destination_root.mkdir(parents=True, exist_ok=True)
        created = 0
        imported_assets: dict[str, MaterialAsset] = {}
        for file_hash, (asset_key, name, group, family, route_variants) in ASSETS.items():
            image = images[file_hash]
            source = (ASSET_ROOT / image["path"]).resolve()
            if not source.is_relative_to(ASSET_ROOT.resolve()) or not source.is_file():
                raise RuntimeError(f"route_asset_missing:{asset_key}")
            if hashlib.sha256(source.read_bytes()).hexdigest() != file_hash:
                raise RuntimeError(f"route_asset_changed:{asset_key}")
            suffix = source.suffix.lower()
            mime_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(suffix)
            if mime_type is None:
                raise RuntimeError(f"route_asset_type_unsupported:{asset_key}:{suffix}")
            destination = destination_root / f"{file_hash}{suffix}"
            if not destination.exists():
                shutil.copyfile(source, destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != file_hash:
                raise RuntimeError(f"managed_asset_changed:{asset_key}")
            media = db.scalar(select(StoredMedia).where(
                StoredMedia.tenant_id == tenant.id,
                StoredMedia.storage_path == str(destination),
            ))
            if media is None:
                media = StoredMedia(
                    tenant_id=tenant.id,
                    original_name=f"{name}{suffix}",
                    media_type="image",
                    mime_type=mime_type,
                    file_size=destination.stat().st_size,
                    storage_path=str(destination),
                    created_by=admin.id,
                )
                db.add(media)
                db.flush()

            asset = db.scalar(select(MaterialAsset).where(
                MaterialAsset.knowledge_version_id == version.id,
                MaterialAsset.asset_key == asset_key,
            ))
            narrative = ASSET_NARRATIVES[asset_key]
            if asset is None:
                asset = MaterialAsset(
                    knowledge_version_id=version.id,
                    asset_key=asset_key,
                    source_path=str(destination),
                    display_name=name,
                    media_type="image",
                    usage=group,
                    file_hash=file_hash,
                    available=True,
                    metadata_json={
                        "stored_media_id": media.id,
                        "content_family": family,
                        "content_group_key": group,
                        "route_variants": route_variants,
                        "review_state": "evaluation_ready",
                        "live_approved": True,
                        "source": "china2go_routes_1_2",
                        "narrative_version": ASSET_NARRATIVE_VERSION,
                        **narrative,
                    },
                )
                db.add(asset)
                created += 1
            else:
                current_metadata = dict(asset.metadata_json or {})
                asset.available = True
                asset.source_path = str(destination)
                asset.display_name = name
                asset.media_type = "image"
                asset.usage = group
                asset.file_hash = file_hash
                asset.metadata_json = {
                    **current_metadata,
                    "stored_media_id": media.id,
                    "content_family": family,
                    "content_group_key": group,
                    "route_variants": route_variants,
                    "review_state": "evaluation_ready",
                    "live_approved": True,
                    "source": "china2go_routes_1_2",
                    "narrative_version": ASSET_NARRATIVE_VERSION,
                    **{
                        key: (
                            value
                            if key == "recommended_caption"
                            and current_metadata.get("narrative_version") != ASSET_NARRATIVE_VERSION
                            else _reviewed_metadata_value(current_metadata, key, value)
                        )
                        for key, value in narrative.items()
                    },
                }
            imported_assets[asset_key] = asset

        for priority, (route, spec) in enumerate(ROUTES.items(), start=1):
            branch = db.scalar(select(RouteBranch).where(
                RouteBranch.knowledge_version_id == version.id,
                RouteBranch.branch_key == ROUTE_BRANCH[route],
            ))
            if branch is None:
                branch = RouteBranch(
                    knowledge_version_id=version.id,
                    branch_key=ROUTE_BRANCH[route],
                    name=spec["name"],
                    description="网站前两条线路的已审核AI回复流程",
                    required_slots=["party_size", "departure_window"],
                    complete=True,
                    priority=priority,
                )
                db.add(branch)
                db.flush()
            else:
                branch.name = spec["name"]
                branch.description = "版本化线路包驱动的AI回复与默认SOP"
                branch.required_slots = spec["required_slots"]
                branch.complete = True
                branch.priority = priority
            ordered_groups = [*spec["sequence"], *(
                key for key in spec["groups"] if key not in spec["sequence"]
            )]
            for order, group_key in enumerate(ordered_groups, start=1):
                group = spec["groups"][group_key]
                node = db.scalar(select(RouteNode).where(
                    RouteNode.route_branch_id == branch.id,
                    RouteNode.node_key == group_key,
                ))
                values = {
                    "node_type": "reply",
                    "content": group["text"],
                    "required_slots": [],
                    "evidence_refs": group["evidence"],
                    "asset_keys": group["assets"],
                    "missing_content": False,
                    "sort_order": order,
                }
                if node is None:
                    db.add(RouteNode(route_branch_id=branch.id, node_key=group_key, **values))
                else:
                    for key, value in values.items():
                        setattr(node, key, value)
        default_sops = [
            _seed_default_sop(db, tenant, admin, package, imported_assets)
            for package in ROUTE_PACKAGES.values()
        ]
        default_sops.append(_seed_unclassified_sop(db, tenant, admin))
        legacy_names = {
            "桃花9日 · 图文演练（测试会话26）",
            "桃花11日 · 图文演练（测试会话26）",
        }
        for old_sop in db.scalars(select(SopDefinition).where(
            SopDefinition.tenant_id == tenant.id,
            SopDefinition.name.in_(legacy_names),
        )).all():
            old_sop.status = "paused"
        legacy = db.scalars(select(MaterialAsset).join(KnowledgeVersion).where(
            KnowledgeVersion.version_key == "materials-20260826-v1"
        )).all()
        for asset in legacy:
            metadata = dict(asset.metadata_json or {})
            family = metadata.get("content_family")
            if family in LEGACY_GROUPS:
                asset.metadata_json = {**metadata, "content_group_key": LEGACY_GROUPS[family]}
        db.commit()
        return {
            "knowledge_version": KNOWLEDGE_VERSION,
            "assets": len(ASSETS),
            "created_assets": created,
            "live_approved": len(ASSETS),
            "default_sop_ids": [sop.id for sop in default_sops],
            "backup_path": backup_path,
            "remote_writes": 0,
        }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run(), ensure_ascii=False))
