"""Apply the approved business document's brand positioning to route assets."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRAND = "我們專門做4至10人小團、全外賓不拼內賓、零購物的西藏行程喔～"


def update(package):
    package['package_version'] = '2026-09-12.business-brand-3'
    package['ai_guidance'] = package['ai_guidance'].replace('4至6人小团', '4至10人精緻小團的公司定位')
    guidance = ' 公司定位依業務文件為4至10人小團，不將特定報價或9座車型推廣為10人適用；多人同行有優惠，不自行編造折扣金額。'
    if guidance not in package['ai_guidance']:
        package['ai_guidance'] += guidance
    fact_id = 'route.shared.brand_positioning'
    package['knowledge_facts'] = [f for f in package['knowledge_facts'] if f['id'] not in {fact_id, 'route.shared.group_offer'}]
    package['knowledge_facts'] += [
        {'id': fact_id, 'text': 'China2Go專門做4至10人小團、全外賓不拼內賓、零購物的西藏行程。這是公司服務定位，不代表所有人數適用同一報價或同一車型。', 'source_ref': 'business-feedback-AI1#brand-positioning'},
        {'id': 'route.shared.group_offer', 'text': '業務文件確認：我們有多人同行的優惠。文件沒有指定優惠金額、比例或具體門檻，不自行計算折扣。', 'source_ref': 'business-feedback-AI1#group-offer'},
    ]
    for fact in package['knowledge_facts']:
        if fact['id'].endswith('.overview'):
            fact['text'] = fact['text'].replace('采用4至6人小团', '公司主打4至10人小團')
        if fact['id'] == 'route.shared.vehicle_reference':
            fact['text'] = '目前4至6人配置使用2025年9座VIP航空座椅車，車內配有緊急醫療氧氣鋼瓶及彌散式供氧設備。公司服務4至10人小團；不能將這台9座車說成可載10位旅客。'
    groups = package['content_groups']
    old_brand = groups['brand_positioning']['approved_text']
    groups['brand_positioning']['approved_text'] = BRAND
    groups['brand_positioning']['evidence_refs'] = [fact_id]
    replacements = {old_brand: BRAND}
    for key in ('party_intro_small', 'party_intro_group', 'party_intro_solo'):
        if key in groups:
            old = groups[key]['approved_text']
            new = old.replace('4至6人', '4至10人')
            replacements[old] = new
            groups[key]['approved_text'] = new
            if old != new and fact_id not in groups[key]['evidence_refs']:
                groups[key]['evidence_refs'].append(fact_id)
    # Keep the vehicle specification, without repeating it as the company size limit.
    old = groups['vehicle_reference']['approved_text']
    new = old.replace('4至6人一團，', '')
    replacements[old] = new
    groups['vehicle_reference']['approved_text'] = new
    if 'route.shared.group_offer' not in groups['price_reference']['evidence_refs']:
        groups['price_reference']['evidence_refs'].append('route.shared.group_offer')
    for answer in package['fixed_answers']:
        if answer['id'] in {'small_party', 'group_party'}:
            answer['status'] = 'active'
            answer['answer_text'] = groups[answer['content_group_key']]['approved_text']
            answer['answer_origin'] = 'operator_approved'
            answer['source_ref'] = 'business-feedback-AI1#brand-positioning'
            answer['fact_ids'] = [fact_id]

    def sync(value):
        if isinstance(value, dict):
            return {k: sync(v) for k,v in value.items()}
        if isinstance(value, list):
            return [sync(v) for v in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    return sync(package)


if __name__ == '__main__':
    for slug in ('peach-9d-2027', 'peach-11d-2027'):
        path = ROOT / 'data/knowledge/china2go/route-packages' / slug / 'route-package.json'
        data = update(json.loads(path.read_text(encoding='utf-8')))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
