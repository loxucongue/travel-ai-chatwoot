"""Business-authored November 5 delta; preserve unlisted route settings."""
from copy import deepcopy

ALI = 'tibet_ali_15d'
VERSION = '20261105-ali-north-central-v1'
SOURCE = '阿里轉山線_AI腳本修改清單（北線＋中線）.docx / 2026-11-05'
NAME = '岡仁波齊精華轉山＋阿里北線＋中線15天'


def update_package(original):
    p = deepcopy(original)
    if p['route_variant'] != ALI or p['package_version'] == VERSION:
        return p
    old_name = p['name']
    p.update(name=NAME, selection_title='阿里北線＋中線15天',
             default_entry_message=NAME, package_version=VERSION)
    p['opening_text_in_introduction'] = True
    p['selection_aliases'] = list(dict.fromkeys(p['selection_aliases'] + [
        '文布南村', '吳如措', '洞措', '當穹措', '那倉卓姆莊園', '阿里北＋中線', '北中線', '北線＋中線']))
    p['intake']['options'] = ['自己一人', '2-3人', '4-5人', '6人（含）以上']
    p['routing_guidance'] = ('阿里、轉山、岡仁波齊、轉湖、古格、藏北，以及文布南村、吳如措、洞措、當穹措、那倉卓姆莊園、北中線，'
        '均可在本15天北線＋中線產品中介紹，不因中線或這些地名就轉訂製。理解客人完整語意；詢問、比較、否定不等於選線。'
        '明確要看本線才開始介紹。只提普通珠峰/拉薩不切阿里；只提納木措/扎西半島先澄清。'
        '12/13天、不轉山、自訂路線仍由顧問規劃；只說12天以上不主動推阿里。')
    p['ai_guidance'] = ('獨立阿里北線＋中線15天系列，拼團4–6人；6位（含）以上走獨立一團介紹後交接，'
        '4–5位不能自動當包團，明確要求包團才走訂製。先問未知人數，再介紹及體能/月分。'
        '首次邀請只索取微信或LINE的ID/QR code，不提官方LINE@。團期及價格保持原公布資料；不套普通西藏優惠。詳讀本線Skill。')
    facts = {f['id'].split('.', 1)[1]: f for f in p['knowledge_facts']}

    def fact(key, text):
        item = {'id': ALI + '.' + key, 'text': text, 'source_ref': SOURCE}
        if key in facts:
            facts[key].update(item)
        else:
            p['knowledge_facts'].append(item)
            facts[key] = item

    def group(key, text, assets=None, initial=True):
        fact(key, text)
        previous = p['content_groups'].get(key, {})
        assets = previous.get('asset_keys', []) if assets is None else assets
        p['content_groups'][key] = {**previous, 'approved_text': text, 'asset_keys': assets,
            'evidence_refs': [ALI + '.' + key], 'initial_delivery': initial,
            'delivery_mode': 'text_then_assets' if assets else 'text_only'}

    group('itinerary_overview', '您好~我是China2Go國旅環球！這條是我們最特別的路線【阿里北線＋中線15天】：轉神山、轉阿里、繞三大聖湖，還會住進文布南村！很多人說是一生一次的朝聖旅程')
    group('north_central_loop', '從拉薩出發，經過珠峰一路往西到神山岡仁波齊，再把阿里北線跟中線一次走完繞回來～南進北出，完全不走回頭路')
    group('wenbu_village', '我們這條是【北線＋中線一次走完】，阿里的精華都在裡面！而且還會住進文布南村喔～', ['ali15_wenbu_village'])
    group('wenbu_manor', '文布南村這晚我們住的是【那倉卓姆莊園】，都是有供應氧氣的', ['ali15_wenbu_manor'])
    group('wenbu_sunset', '你看！很有藏式風情，傍晚在莊園前面就能看著夕陽慢慢落在當惹雍措的湖面上，對岸就是達果雪山，是整趟旅程最特別的一晚')
    group('north_central_lakes', '我們的行程也是坊間比較少見的【轉山＋三大聖湖＋阿里北線＋中線】～')
    group('wuma', '一措再措中有很多特色湖，像物瑪措是高原鹽湖，湖水是很漂亮的漸層藍')
    group('dangra_lake', '而阿里中線也是大家很想去但是很難去的，我們會到苯教第一神湖當惹雍措，晚上就住在湖邊的文布南村！', ['ali15_dangra_lake'])
    group('wildlife', '藏北高原人煙稀少，從改則一路到色林措，常常可以看到藏羚羊、藏野驢、野牦牛，運氣好還會遇到黑頸鶴～大家都會一直喊司機停車拍照')
    group('followup_yaks', '走阿里北線跟中線，一路上常常是一整片湖、一大群牦牛跟雪山在同一個畫面裡～這種遼闊真的只有來過才懂', initial=False)
    group('followup_wenbu', '文布南村是當惹雍措旁的【象雄古村】，村裡還留著很多石頭砌的老房子，走在裡面常常會遇到羊在門口晃來晃去～這種很生活的藏地村落，因為比較深度，所以一般旅行團比較少會來！', ['ali15_followup_wenbu'], False)
    itinerary = facts['daily_itinerary']['text']
    for old, new in [
        ('塔欽Kailas酒店', '塔欽Kailas萬豪國際酒店'),
        ('羌塘無人區沿路可能看到野生動物', '藏北沿路可能看到野生動物'),
        ('羌塘荒原、大地之樹、當惹雍措 → 尼瑪', '洞措（下車親水）；當穹措、當惹雍措遠觀 → 文布南村（象雄古村）'),
        ('尼瑪雲天洲際酒店（供氧）', '那倉卓姆莊園（文布南村，藏式莊園，彌散式供氧）'),
        ('達則措、恰規措、色林措 → 班戈，藏北星空', '尼瑪 → 達則措（遠觀）→ 大地之樹 → 恰規措 → 吳如措（遠觀）→ 色林措（親水）→ 班戈，藏北星空')]:
        if old not in itinerary:
            raise ValueError('unexpected_itinerary_baseline: ' + old)
        itinerary = itinerary.replace(old, new)
    fact('daily_itinerary', itinerary)
    fact('inclusions', facts['inclusions']['text'] + ' 另含哈達、礦泉水、布宮景觀下午茶、旱地拔蔥影片、撒隆達、掛經幡；China2Go阿里大禮包（隨身氧氣瓶、葡萄糖、鼻吸管、濕紙巾、青稞酥、特色零食）。')
    fact('alternatives', '15天已包含北線＋中線及文布南村；不是未發布的13天中線。12天、不轉山版及13天完整腳本仍未發布。客人說去過了，記錄去過哪些地方，了解未知天數、人數後請阿里顧問訂製；不套15天拼團價。只有同意才轉普通10天。')
    fact('wildlife_faq', '野生動物不能保證，但藏北這段遇到藏羚羊、藏野驢的機會很高，司機看到都會幫大家停下來拍。')
    fact('shopping_faq', '保證全程純玩，不安排購物店、不進套路店，也沒有自費項目！如果有違約，我們賠您人民幣5,000元～。此處指無安排自費遊覽項目；馬匹、背夫、個人消費等仍按不含費用清單，不代表所有個人服務免費。')
    fact('party_intake', '4–5人：我們的拼團是4-6人～擁有西藏最多的新小車資源，車子跟高原司機都能直接幫您們安排好，你們一起來玩最適合了。6人（含）以上：你們人數多其實非常適合走阿里！我們有最新的航空VIP車型可以推薦給您～先給您簡單介紹一下行程，因為自己一團，路線跟天數都可以再依你們調整。訂製客不足4位可問：如果能湊到4位一起，價格會比較划算喔～您身邊有朋友也想一起去嗎？不是不滿4位就拒絕。')
    sequence = p['content_sequence']
    sequence[sequence.index('itinerary_overview')+1:sequence.index('itinerary_overview')+1] = ['north_central_loop', 'wenbu_village', 'wenbu_manor', 'wenbu_sunset']
    sequence.insert(sequence.index('wuma'), 'north_central_lakes')
    sequence.insert(sequence.index('wildlife'), 'dangra_lake')
    for script in p['fixed_answers']:
        script['answer_text'] = script['answer_text'].replace(old_name, NAME)
    policy = p['followup_policy']
    policy['daily_sequence'].append('followup_wenbu')
    policy['guidance'] = policy['guidance'].replace('最多七則', '最多八則')
    large = p['large_party']
    large['minimum'] = 6
    large['handoff_label'] = '高價值・阿里獨立成團'
    large['explicit_private_label'] = '高價值・阿里訂製'
    large['source_ref'] = SOURCE
    large['opening_text'] = '您們{party_description}自己一團，我們有最新的航空VIP車型可以推薦給您～先給您簡單介紹一下行程，路線跟天數都可以再依你們調整。'
    large['contact_text'] = ('剛剛看的是基本行程，出發日期、天數、要不要每個人都轉山都可以再調整～這部分細節比較多，在這裡來來回回不太好討論，方便給我您微信或LINE的QR code或ID嗎？這樣傳送行程、圖片跟轉山裝備資料，資料不會被洗掉也不會傳不出去～加入後記得跟我說一下您是誰～前面的問題就不用再問一次啦～')
    large['completed_text'] = '剛剛看的是基本行程，你們自己一團，出發日期、天數、要不要每個人都轉山都可以再調整～我們有最新的航空VIP車型可以推薦給您，細節由阿里顧問一起確認。'
    large['introduction_sequence'] = ['itinerary_overview', 'north_central_loop', 'wenbu_village', 'wenbu_manor', 'wenbu_sunset', 'private_highlight', 'private_guests', 'private_advantage']
    large['brief_sequence'] = list(large['introduction_sequence'])
    p['profile_labels'] = {'guest_segment': {
        'experienced': ['高價值・深度玩家', '高價值・阿里訂製'],
        'custom': ['高價值・阿里訂製']}}
    return p
