"""Correct known authoring text in active packages, preserving other edits.

This is a one-time data migration, never a customer-output rewriting step.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys


def corrected_package(package):
    result = deepcopy(package)
    eleven = result['route_variant'] == 'peach_11d_2027'
    old_hotel = '以及除了地區條件有限以外，我們全面升級都住國際品牌希爾頓飯店哦！'
    new_hotel = ('除了波密和珠峰地區以外，其他住宿安排都升級為國際品牌希爾頓飯店喔！'
                 if eleven else '除了波密地區以外，其他住宿安排都升級為國際品牌希爾頓飯店喔！')

    def text(value):
        return (value.replace(old_hotel, new_hotel)
                .replace('2人1標間拼住價格，如需單休須補單房差', '兩人一房的價格，一個人住一間須補單房差')
                .replace('十人小團優惠價：', '桃花11日團費：' if eleven else '標準6人小團：')
                .replace('★限量升級4-6人小團 價格不變', '可接待4–6人小團，其他人數的實際報價由顧問確認。' if not eleven else '4–6人小團，兩人一房。')
                .replace('比較自在', '輕鬆自在')
                .replace('方便留一下您的LINE ID嗎？我請顧問接著協助您確認出發安排。', '我可以請顧問接著協助您確認出發安排。方便留一下您的LINE ID嗎？')
                .replace('這路線是3月尾-4月出，每周4個出團日。請問您計劃什麼時候來呢？好安排顧問給您詳細介紹~❤️',
                         '這條行程是3/20–4/10出發，每週五、六、日、一發團。請問您大概計劃什麼時候出發呢？'))

    # Update the three authoring views together. Image order, prices, intervals
    # and unrelated operator text are retained.
    for fact in result['knowledge_facts']:
        fact['text'] = text(fact['text'])
    for group in result['content_groups'].values():
        group['approved_text'] = text(group['approved_text'])
    greeting = result['content_groups'].get('advisor_greeting')
    if greeting and greeting['approved_text'].startswith('接下來帶您看桃花'):
        greeting['approved_text'] = '我們專門做4–10人小團、全外賓不拼內賓、零購物的西藏行程！\n' + greeting['approved_text']
    # Product introduction is data, not model-generated ordering.
    if eleven:
        sequence = result['content_sequence']
        if 'hotel_reference' in sequence and 'rongbuk_upgrade' in sequence:
            sequence.remove('hotel_reference')
            sequence.insert(sequence.index('rongbuk_upgrade'), 'hotel_reference')
        if 'peach_highlights' in sequence and 'hotel_reference' in sequence:
            sequence.remove('peach_highlights')
            sequence.insert(sequence.index('hotel_reference'), 'peach_highlights')
    result['ai_guidance'] = '以AI.docx、AI(1).docx、AI(2).docx的业务答复口径为准；官网补充未覆盖的线路事实。优先使用适用原话，按content_sequence发完整介绍，再回答期间的问题。需要询问时，把一个必要问题放在整轮最后。'
    for script in result['fixed_answers']:
        original_text = script['answer_text']
        script['answer_text'] = text(script['answer_text'])
        if not eleven and script['id'] == 'price':
            script['usage_note'] = '9日9980元仅为标准6人小团、两人一房报价。客户人数不是6人或人数未知时，说明标准价格及适用条件，其人数报价需要顾问确认，不把9980直接乘以2、4、8或10当成其报价。6人且两人一房时才可算全团59880元；单问单房差直接答2600元。只问团费不额外列全团合计。'
            script['source_ref'] = 'AI(1).docx#价格建议；AI(2).docx#价格适用条件'
        if not eleven and script['id'] == 'group_party':
            script['usage_note'] = '适用时保留公司定位、4–6人团型及可考虑自己一团的原话主体。介绍完成后去掉先介绍行程，不缩成收到几位。自己一团的安排和报价需另外确认，不从团型推导4位与标准6人报价一样。'
        if script['id'] == 'age_75_entry_claim' and script['answer_text'] in (
            '目前75歲以上長輩申請入藏函是申請不下來的',
            '台灣旅客65–75歲（含75歲）可以報名，需提供健康證明；超過75歲則不建議參加，需要個案確認。'):
            script.update(
                name='满75岁台湾旅客',
                answer_text='台灣旅客65–75歲（含75歲）可以報名，使用台胞證需要提供健康證明喔。',
                source_ref='AI(2).docx#第21段；业务确认2026-09-30',
                answer_origin='operator_approved',
                positive_examples=['妈妈刚好75岁可以去吗，需要什么证明', '75岁台湾旅客能报名吗'],
                negative_examples=['76岁可以去吗', '64岁是最低年龄吗', '香港护照的规定'],
                usage_note='完整短答已经覆盖满75岁资格和证明，直接引用即可。不追加健康证明与入藏函一起办理等未提供流程。超过75岁用通用 over_75_reception。')
        if script['id'] == 'senior_health_document':
            old = '是這樣的～目前用台胞證進入西藏的旅客，65歲以上都是要辦理健康證明，不過放心～我到時候幫您備註下～健康證明的格式還有申請規定，我再發給您！也都會依照時間提醒您辦理'
            if script['answer_text'] == old:
                script['answer_text'] = '使用台胞證的旅客，65歲以上需要辦理健康證明喔。'
                script['usage_note'] = '只回答健康证明条件，不反推64岁免交证明；不自行承诺后续发送格式或定时提醒。报名年龄上限另看年龄口径。'
        if script['answer_text'] != original_text:
            script['answer_origin'] = 'operator_approved'
    if not any(s['id'] == 'altitude_concern' for s in result['fixed_answers']):
        days = 11 if eleven else 9
        opening = ('11日會上珠峰，海拔較高。' if eleven else '9日不上珠峰，從林芝入藏，讓身體逐步適應海拔。')
        result['fixed_answers'].append(dict(
            id='altitude_concern', name='担心高反与珠峰安排', status='active',
            priority=130, topics=['altitude'], party_size_min=None, party_size_max=None,
            content_group_key='altitude_health',
            answer_text=opening + '車上和希爾頓房內有供氧設備，但供氧不代表不會高反；出發前可以帶行程請醫師評估身體狀況喔。',
            fact_ids=[f'route.{days}.overview', 'route.shared.hotel_reference', 'route.shared.vehicle_reference'],
            asset_ids=[], source_ref='AI(1).docx#高反建议；线路行程与供氧事实', answer_origin='operator_approved',
            positive_examples=['第一次去担心高反，会不会上珠峰', '最担心高反，这条行程怎么样'],
            negative_examples=['已经不舒服需要帮助', '有随团医师吗', '具体怎么用药'],
            usage_note='这是高反顾虑的简短首答，不再附加全套设备、浓度、医院、保险清单；后续问哪项再回答哪项。'))
    return result


def corrected_configuration(configuration):
    """One-time authoring migration; never rewrites model output."""
    result = deepcopy(configuration)
    replacements = {
        '您先和家人討論，時間還不用急著決定。方便留一下您的 LINE ID 嗎？之後有想調整的地方，可以請顧問接著協助您。':
        '您先和家人討論，時間還不用急著決定。之後有想調整的地方，可以請顧問接著協助您。方便留一下您的 LINE ID 嗎？',
        '這個月份的安排需要另外確認。方便留一下您的 LINE ID 嗎？我請顧問協助您看適合的行程。':
        '這個月份的安排需要另外確認，可以請顧問協助您看適合的行程。方便留一下您的 LINE ID 嗎？',
        '可以呀，留 Email 就好。方便提供您的信箱嗎？我請顧問用郵件和您聯絡。':
        '可以呀，留 Email 就好，我可以請顧問用郵件和您聯絡。方便提供您的信箱嗎？',
    }
    for script in result.get('common_scripts', []):
        script['text'] = replacements.get(script['text'], script['text'])
    return result


def main():
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.route_packages import load_route_packages, install_route_package
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    for key, before in list(load_route_packages().items()):
        after = corrected_package(before)
        changed = before != after
        if changed and args.apply:
            if args.backup is None:
                parser.error('--apply requires --backup')
            args.backup.mkdir(parents=True, exist_ok=True)
            with (args.backup / (key + '.json')).open('x', encoding='utf-8') as file:
                json.dump(before, file, ensure_ascii=False, indent=2)
            after['package_version'] += '.business-20261004'
            install_route_package(after)
        print(json.dumps({'route': key, 'changed': changed, 'applied': changed and args.apply}))


if __name__ == '__main__':
    main()
