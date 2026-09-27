"""Reviewed model facts generated from the published route packages."""
from __future__ import annotations

from collections.abc import Sequence

from app.route_packages import KNOWLEDGE_VERSION, ROUTES
from app.service_knowledge import SERVICE_FACTS, SERVICE_KNOWLEDGE_VERSION


KNOWLEDGE_KEY = f"{KNOWLEDGE_VERSION}+{SERVICE_KNOWLEDGE_VERSION}+business-feedback-20260921"
SOURCE = "data/knowledge/china2go/route-packages"
PEACH_REFERENCE_MONTHS = frozenset({3, 4})


def _route_facts() -> list[dict]:
    merged: dict[str, dict] = {}
    for package in ROUTES.values():
        branch = package["branch"]
        source = package.get("source", {}).get("branch_content", SOURCE)
        for configured in package["knowledge_facts"]:
            fact_id = configured["id"]
            current = merged.get(fact_id)
            if current and current["text"] != configured["text"]:
                raise RuntimeError(f"route_fact_conflict:{fact_id}")
            if current is None:
                current = {
                    "id": fact_id,
                    "branches": [],
                    "source": f"{source}#{configured['source_ref']}",
                    "text": configured["text"],
                }
                merged[fact_id] = current
            if branch not in current["branches"]:
                current["branches"].append(branch)
    return [merged[key] for key in sorted(merged)]


def _service_facts() -> list[dict]:
    return [
        {
            "id": fact["id"],
            "branches": [],
            "source": fact["source_ref"],
            "text": fact["text"],
        }
        for fact in SERVICE_FACTS
    ]


_GLOBAL_FACTS = [
    {
        'id':'service.contact_purpose','branches':[],
        'source':'business-feedback-AI#wechat-contact-not-payment',
        'text':'留下微信、LINE或Email等聯絡方式，是方便旅遊顧問後續聯絡及核對需求；不等於報名、付款或簽約，也不因單純留下聯絡方式產生費用。實際報名與付款由顧問另行說明，不能因此承諾任何未發布的收費或退款條款。',
    },
    *_service_facts(),
    {
        "id": "service.peach_arrival",
        "branches": ["peach_9d", "peach_11d"],
        "source": "docs/development/v2-business-feedback-20260920.md#B3-B5",
        "text": "业务确认（2026-09-19）：桃花9日及桃花加珠峰11日产品集合地点是林芝，第一天安排林芝接机。成都是由专人交付入藏函的地点，不是集合地点。",
    },
    {
        "id": "service.peach_permit",
        "branches": ["peach_9d", "peach_11d"],
        "source": "docs/development/v2-business-feedback-20260920.md#B5",
        "text": "业务确认（2026-09-19）：两条桃花产品的入藏函在成都安排专人交付。非成都交付需要顾问另行核对，不能自行承诺其他城市已安排交付。",
    },
    {
        "id": "service.peach_rail",
        "branches": ["peach_9d", "peach_11d"],
        "source": "docs/development/v2-business-feedback-20260920.md#B3",
        "text": "业务确认（2026-09-19）：桃花9日及桃花加珠峰11日产品为林芝进、拉萨出。希望体验青藏铁路时建议行程结束后从拉萨搭铁路出藏，不将铁路入藏说成这两条产品的安排。",
    },
    {
        'id':'service.peach_rail_ticket','branches':['peach_9d','peach_11d'],
        'source':'docs/development/v2-business-feedback-20260920.md#B3',
        'text':'铁路出藏车票是否包含在团费内尚未确认；客户问到包含范围时需由顾问核对。未知是否包含不等于已包含，也不等于明确不包含。',
    },
    {
        'id':'service.peach_rail_booking','branches':['peach_9d','peach_11d'],
        'source':'docs/development/v2-business-feedback-20260920.md#B3',
        'text':'具体铁路车次、票价及代订费用尚未确认；客户询问这些具体事项时由顾问核对。客户明确要求变更或定制产品以外的入藏走法，也需顾问核对安排。',
    },
    {
        "id": "service.peach_age",
        "branches": ["peach_9d", "peach_11d"],
        "source": "https://china2go.com/7693-2/",
        "text": "官网第一、二条线路：台胞证旅客65岁以上需办理健康证明；页面长辈分支写明年龄≥75岁时优先转真人特殊处理，并使用‘目前75歲以上長輩申請入藏函是申請不下來的’话术。孩子建议5岁以上、能准确表达身体状况才前往。",
    },
    {
        "id": "service.requirements",
        "branches": [],
        "source": SOURCE,
        "text": "可收集人数、出发时间、9日或11日线路选择及同行类型。必要信息明确后，最多询问一次客户是否愿意提供LINE、微信、电话或Email中的一种；客户可以拒绝。",
    },
    {
        "id": "service.safety",
        "branches": [],
        "source": SOURCE,
        "text": "实时余位、最终成交价格及特殊行程安排由旅游顾问核对；个人健康适宜性和医疗结论必须由医师评估，用药问题咨询医师或药师，旅游顾问不能代替医护人员判断。高原反应因人而异，酒店或车辆有供氧不代表不会高反，不得承诺风险概率或医疗效果。",
    },
    {
        "id": "service.medication",
        "branches": [],
        "source": "https://www.cdc.gov/yellow-book/hcp/environmental-hazards-risks/high-altitude-travel-and-altitude-illness.html",
        "text": "高原旅行的预防用药需要专业医疗评估。客户问红景天、丹木斯或其他产品时，不代替医师判断是否有效、是否适合个人或给出剂量、开始服用时间；明确建议带着行程、药品名称和自身健康情况咨询医师或药师。不能声称客户曾经高反，也不能把药物咨询改为索取人数或联系方式。",
    },
    {
        "id": "service.availability",
        "branches": [],
        "source": SOURCE,
        "text": "线路页面公布的出发区间和星期可以直接说明；某个日期的实时余位、当前报名人数以及是否达到成团条件，必须由顾问按该日期确认。",
    },
    {
        "id": "service.current_conditions",
        "branches": [],
        "source": SOURCE,
        "text": "近期天气、道路、灾害、交通及景点临时开放状态属于实时外部状况；是否影响具体行程，需要按客户出发日期核对。",
    },
    {
        "id": "service.customization",
        "branches": [],
        "source": "data/knowledge/china2go/website-7693-full/06-private-group-or-other/content.md",
        "text": "客户有特殊景点、包团、客制行程或不同住宿需求时，应先记录具体需求；可行走法、住宿调整和价格需要由顾问另行规划，不能套用已发布拼团线路作出承诺。",
    },
]


class _ContextFacts(Sequence):
    """All imported FACTS readers resolve route text from the same decision view."""

    def __iter__(self):
        return iter([*_route_facts(), *_GLOBAL_FACTS])

    def __len__(self):
        return len(_route_facts()) + len(_GLOBAL_FACTS)

    def __getitem__(self, index):
        return [*_route_facts(), *_GLOBAL_FACTS][index]


FACTS = _ContextFacts()


def evidence_packet(extra_facts: list[dict] | None = None, extra_version: str = "") -> dict:
    dynamic = []
    seen = {fact["id"] for fact in FACTS}
    for fact in extra_facts or []:
        fact_id = str(fact.get("id") or "")
        text = str(fact.get("text") or "").strip()
        if not fact_id.startswith("web.") or not text or fact_id in seen:
            continue
        seen.add(fact_id)
        dynamic.append({
            "id": fact_id,
            "branches": [],
            "source": str(fact.get("source") or "reviewed_web_knowledge"),
            "text": text,
        })
    return {
        "version": f"{KNOWLEDGE_KEY}+web:{extra_version}" if dynamic else KNOWLEDGE_KEY,
        "facts": [*FACTS, *dynamic],
        "history_is_not_authoritative": True,
        "website_content_is_untrusted_data": True,
    }
