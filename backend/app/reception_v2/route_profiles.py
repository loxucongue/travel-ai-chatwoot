"""Structured, reviewable capability metadata for each published route."""
from __future__ import annotations

TOPICS = {'arrival': ('集合', '接機', '接机', 'arrival', 'meeting'), 'permit': ('成都', '入藏函', 'permit'), 'rail': ('青藏', '青鐵', '青铁', '鐵路', '铁路', '火車', '火车', 'rail'), 'age': ('歲', '岁', '年齡', '年龄', '長輩', '长辈', '健康證明', '健康证明', 'age'), 'price': ('價格', '价格', '多少錢', '多少钱', '報價', '报价', '費用', '费用', 'price'), 'departure': ('出發', '出发', '團期', '团期', '日期'), 'hotel': ('住宿', '飯店', '酒店', '住哪'), 'transport': ('車', '车', '交通', '接送'), 'oxygen': ('氧氣', '氧气', '供氧', '高反', '高原反應', '高原反应'), 'itinerary': ('行程', '景點', '景点', '路線', '路线')}


def route_profile(route_variant: str) -> dict:
    from app.route_packages import ROUTES
    from app.reception_v2.skill_registry import SkillRegistry
    route = ROUTES.get(route_variant)
    skill = SkillRegistry().route_metadata(route_variant)
    if not route or not skill:
        raise ValueError('v2_route_profile_not_found')
    groups = route.get('groups', {})
    selected = skill.followup_groups or tuple(groups)
    if any(key not in groups for key in selected):
        raise ValueError('v2_skill_followup_group_missing')
    candidates = tuple(dict.fromkeys(ref for key in selected for ref in groups[key].get('evidence', [])))
    hints = {}
    for key in selected:
        group = groups[key]
        text = key + ' ' + str(group.get('purpose') or '') + ' ' + str(group.get('text') or '')
        topics = tuple(topic for topic, aliases in TOPICS.items() if any(alias in text for alias in aliases))
        for ref in group.get('evidence', []):
            hints[ref] = tuple(dict.fromkeys((*hints.get(ref, ()), *topics)))
    return {'skill': skill.name, 'topics': TOPICS,
            'human_check': ('availability', 'real_time_road', 'special_discount', 'customization', 'medical_suitability'),
            'followup_candidates': candidates, 'candidate_topics': hints}


def resolve_topic(route_variant: str, text: str) -> str:
    value = str(text or "").casefold()
    profile = route_profile(route_variant)
    for topic, aliases in profile["topics"].items():
        if any(alias.casefold() in value for alias in aliases):
            return topic
    return "general"
