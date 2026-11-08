"""Remove conflicting legacy presentation guidance without changing price policy."""
from copy import deepcopy


def update_package(package):
    result = deepcopy(package)
    if result.get('route_variant') != 'tibet_overview_10d':
        return result
    text = result.get('ai_guidance', '')
    replacements = {
        '報價後接輕鬆問句；': '答完本輪報價即可，不強制追加問題；',
        '保留團費、兩人一間、包含、不含和報價後的輕鬆問句。':
            '首次報價用短行列團費、兩人一房及共同包含／不含；後續只改人數時只報新方案與優惠，不重複完整清單。價格與交通一起問時，報價後只展開交通。',
        '僅說一位旅客不等於要求單住，不主動補房差。':
            '一位旅客不等於必須單住；單人報價或接受拼房時，要提醒未拼成仍需補本線房差，不能保證免費拼房成功。',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    for fact in result.get('knowledge_facts', []):
        if fact.get('id') == 'tibet_overview_10d.group':
            fact['source_ref'] = '麗麗2026-11-08多人優惠及獨立成團確認；現行成團門檻配置'
            fact['text'] = ('拼團滿2人發團、車輛容量最多6人，使用9座VIP車、兩人一房。'
                '車輛容量不是報名團型：本線6位一起報名按獨立成團安排、不拼其他旅客，可另選出發日期；'
                '4位可比較拼團與獨團。多人優惠與獨團加價均按group_pricing及日期由核價工具計算，'
                '不能把6位說成未折扣的普通拼團，也不能把季節基準價當最終價。'
                '5位獨團及7位以上的完整成本尚未配置，交顧問核價，不借4位或6位價格代答。')
    transport_text = '交通包含：9座VIP車、司機、接送機及回程拉日鐵路票。\n不含往返西藏的機票或火車票。'
    facts = result.setdefault('knowledge_facts', [])
    if not any(f.get('id') == 'tibet_overview_10d.transport' for f in facts):
        facts.append({'id': 'tibet_overview_10d.transport', 'text': transport_text,
                      'source_ref': '既有tibet_overview_10d.included與excluded交通範圍摘錄'})
    scripts = result.setdefault('fixed_answers', [])
    if not any(x.get('id') == 'transport_scope' for x in scripts):
        scripts.append({'id': 'transport_scope', 'status': 'active', 'name': '交通包含與不含（單項短答）',
                        'source_ref': '既有included與excluded交通範圍摘錄',
                        'content_group_key': 'price_reference', 'topics': ['transport'], 'answer_scope': 'single_topic',
                        'fact_ids': ['tibet_overview_10d.transport'],
                        'answer_text': transport_text, 'asset_ids': [],
                        'positive_examples': ['交通包含什麼？', '包含哪些交通？', '只想問交通安排'],
                        'negative_examples': ['機票多少錢？', '請給我完整費用包含清單'],
                        'usage_note': '回答交通範圍時適用；與價格同問可在報價後引用。只問交通不附其他包含／不含。'})
    result['ai_guidance'] = text
    return result
