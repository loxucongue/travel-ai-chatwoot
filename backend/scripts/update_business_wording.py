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
                .replace('2人1標間拼住價格，如需單休須補單房差', '兩人一房的價格，一個人住一間須補單房差'))

    # Update the three authoring views together. Image order, prices, intervals
    # and unrelated operator text are retained.
    for fact in result['knowledge_facts']:
        fact['text'] = text(fact['text'])
    for group in result['content_groups'].values():
        group['approved_text'] = text(group['approved_text'])
    for script in result['fixed_answers']:
        original_text = script['answer_text']
        script['answer_text'] = text(script['answer_text'])
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
            after['package_version'] += '.business-20260930'
            install_route_package(after)
        print(json.dumps({'route': key, 'changed': changed, 'applied': changed and args.apply}))


if __name__ == '__main__':
    main()
