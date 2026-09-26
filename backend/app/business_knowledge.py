from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeVersion, MaterialAsset, RouteBranch, RouteNode, Tenant


KNOWLEDGE_VERSION = "linzhi-peach-2026-08-24-v1"
FILTER_VERSION = "peach-keywords-v1"
PEACH_KEYWORDS = ["桃花", "桃花节", "林芝", "珠峰", "西藏", "拉萨", "波密", "嘎拉", "帕邦喀", "9日", "11日"]

BRANCHES = [
    {"key": "peach_11d", "name": "桃花加珠峰 11 日", "complete": True, "slots": ["party_size", "departure_window"], "description": "林芝桃花并前往珠峰的 11 日路线。"},
    {"key": "peach_9d", "name": "桃花 9 日", "complete": True, "slots": ["party_size", "departure_window"], "description": "林芝桃花、不上珠峰的 9 日路线。"},
    {"key": "private_group", "name": "自组包团", "complete": False, "slots": ["party_size", "departure_window", "budget"], "description": "只收集基本需求，随后转人工报价。"},
    {"key": "other_peak", "name": "其他时间且上珠峰", "complete": False, "slots": ["departure_window", "party_size"], "description": "非桃花主档期的珠峰需求，缺少内容时转人工。"},
    {"key": "other_no_peak", "name": "其他时间且不上珠峰", "complete": False, "slots": ["departure_window", "party_size"], "description": "非桃花主档期且不上珠峰，缺少内容时转人工。"},
    {"key": "other_destination", "name": "其他目的地", "complete": False, "slots": ["destination", "party_size", "departure_window"], "description": "收集目的地与基本需求后转人工。"},
]

HIGH_RISK_TERMS = {
    "live_price": ["价格", "價格", "报价", "報價", "多少钱", "多少錢", "费用", "費用", "优惠", "優惠"],
    "availability": ["余位", "餘位", "库存", "庫存", "还有位", "還有位", "能不能订", "能不能訂", "一樣出團", "一样出团", "確定出團", "确定出团", "保證出團", "保证出团"],
    "departure_confirmation": ["团期", "團期", "发团", "發團", "确定日期", "確定日期", "哪天出发", "哪天出發"],
    "health_or_permit": ["入藏函", "年龄限制", "年齡限制", "健康证明", "健康證明", "高反", "医疗", "醫療", "医生", "醫生"],
    "fatigue_or_altitude": ["太累", "怕累", "擔心體力", "担心体力", "身體是否適合", "身体是否适合"],
    "contract": ["合同", "退款", "赔偿", "賠償", "保证", "保證", "承诺", "承諾"],
    "payment_terms": ["訂金", "订金", "定金", "付款方式", "付款條件", "付款条件", "報到地點", "报到地点", "在哪報到", "在哪报到"],
}

UNSUPPORTED_PRODUCT_TERMS = [
    "北京行程", "川西", "色達", "色达", "青甘", "阿里", "新疆", "雲南", "云南", "九寨",
    "稻城", "亞丁", "亚丁", "梅里雪山", "麗江", "丽江", "10日定制", "10天定制",
]

COMPLAINT_TERMS = ["投诉", "退款", "骗人", "不满意", "客诉", "找人工", "真人客服"]


def contains_peach_topic(text: str) -> bool:
    normalized = text.casefold()
    return any(keyword.casefold() in normalized for keyword in PEACH_KEYWORDS)


def detect_safety_flags(text: str) -> list[str]:
    flags = [code for code, terms in HIGH_RISK_TERMS.items() if any(term in text for term in terms)]
    if re.search(r"高[\W_]{0,2}反", text) and "health_or_permit" not in flags:
        flags.append("health_or_permit")
    return flags


def deterministic_branch(text: str) -> str:
    value = text.replace(" ", "")
    if any(term in value for term in ["包团", "包車", "包车", "自己组团", "自組團"]):
        return "private_group"
    if any(term in value for term in ["云南", "雲南", "色达", "色達", "北京", "上海", "苏州", "蘇州", "杭州"]):
        return "other_destination"
    if any(term in value for term in ["不上珠峰", "不去珠峰", "不要珠峰"]):
        return "other_no_peak"
    if "11日" in value or "11天" in value or ("珠峰" in value and ("桃花" in value or "林芝" in value)):
        return "peach_11d"
    if "9日" in value or "9天" in value or (("桃花" in value or "林芝" in value) and "珠峰" not in value):
        return "peach_9d"
    if "珠峰" in value:
        return "other_peak"
    if any(term in value for term in ["西藏", "拉萨", "拉薩"]):
        return "other_no_peak"
    return "other_destination"


def deterministic_guard(text: str, branch: str) -> tuple[bool, str | None, list[str]]:
    flags = detect_safety_flags(text)
    if any(term in text for term in COMPLAINT_TERMS):
        return True, "complaint_or_human_requested", sorted(set(flags + ["human_required"]))
    if flags:
        return True, flags[0], flags
    if any(term in text for term in UNSUPPORTED_PRODUCT_TERMS):
        return True, "unsupported_product", ["missing_approved_content"]
    if branch in {"private_group", "other_peak", "other_no_peak", "other_destination"}:
        return True, "business_content_incomplete", ["missing_approved_content"]
    return False, None, []


def _asset_alias(name: str) -> tuple[str, str]:
    mappings = [
        ("9日行程", "peach_9d_itinerary"), ("11天", "peach_11d_itinerary"), ("絨布", "rongbuk_hotel_room"),
        ("帕邦", "pabongka_peach"), ("秀巴", "xiuba_fort_peach"), ("希爾頓酒店", "hilton_room"),
        ("希爾頓製氧", "hilton_oxygen_room"), ("車子照", "vip_vehicle"), ("製氧機", "vehicle_oxygen_unit"),
        ("住宿", "hotel_room_secondary"), ("布達拉", "potala_palace"), ("八廓", "barkhor_street"), ("扎基", "zaki_temple"),
    ]
    for needle, alias in mappings:
        if needle in name:
            return alias, needle
    return f"asset_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]}", "unclassified"


def seed_business_knowledge(db: Session) -> KnowledgeVersion:
    existing = db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.version_key == KNOWLEDGE_VERSION))
    if existing:
        for asset in db.scalars(select(MaterialAsset).where(MaterialAsset.knowledge_version_id == existing.id)).all():
            asset.available = Path(asset.source_path).is_file()
        return existing
    tenant = db.scalar(select(Tenant))
    source = {"materials_dir": settings.business_materials_dir, "business_page": "https://china2go.com/7693-2/", "password_stored": False}
    digest = hashlib.sha256(repr((BRANCHES, source)).encode("utf-8")).hexdigest()
    version = KnowledgeVersion(tenant_id=tenant.id, version_key=KNOWLEDGE_VERSION, title="林芝桃花 AI 试点评测知识", source_summary=source, content_hash=digest)
    db.add(version)
    db.flush()
    for priority, spec in enumerate(BRANCHES, start=1):
        branch = RouteBranch(knowledge_version_id=version.id, branch_key=spec["key"], name=spec["name"], description=spec["description"], required_slots=spec["slots"], complete=spec["complete"], priority=priority)
        db.add(branch)
        db.flush()
        db.add(RouteNode(route_branch_id=branch.id, node_key="evaluate", node_type="reply" if spec["complete"] else "handoff", content=spec["description"], required_slots=spec["slots"], evidence_refs=["business_page", "materials_manifest"], missing_content=not spec["complete"], sort_order=1))

    root = Path(settings.business_materials_dir)
    seen_hashes: set[str] = set()
    seen_aliases: set[str] = set()
    if root.exists():
        for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: str(item)):
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if file_hash in seen_hashes:
                continue
            seen_hashes.add(file_hash)
            alias, usage = _asset_alias(path.name)
            if alias in seen_aliases:
                alias = f"{alias}_{file_hash[:12]}"
            seen_aliases.add(alias)
            db.add(MaterialAsset(knowledge_version_id=version.id, asset_key=alias, source_path=str(path), display_name=path.name, media_type="image", usage=usage, file_hash=file_hash, available=True, metadata_json={"extension": path.suffix.lower(), "size": path.stat().st_size}))
    db.flush()
    return version
