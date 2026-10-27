"""Install operator-provided quotation and first-person copy, preserving route facts."""
from copy import deepcopy
import json
from pathlib import Path
import sys

CONTACT = '我們可以接著協助您確認出發安排。方便傳一下您的 LINE QR Code 給我嗎？'
INCLUDES = '包含：高原保障險、入藏文件、全程供氧住宿（含早餐）、9座VIP供氧車、導遊、門票'
EXCLUDES = '不含：機票、午晚餐、小費、個人花費'


def quotation(days, price, promotion=False):
    heading = f'西藏桃花節{days}日遊' + ('（不上珠峰）' if days == 9 else '')
    label = '拼團升級「4-6人小團」優惠價' if promotion else '「4-6人小團」團費'
    validity = '\n（截至10/31前報名，國慶月優惠）' if promotion else ''
    return f'{heading}\n{label}：人民幣¥ {price:,} /人{validity}\n\n{INCLUDES}\n\n{EXCLUDES}'


def update_package(package):
    result = deepcopy(package)
    # The newer head-count policy supersedes this historical promotion.
    if result.get('party_pricing'):
        return result
    route = result['route_variant']
    if route not in ('peach_9d_2027', 'peach_11d_2027'):
        return result
    days = 11 if route == 'peach_11d_2027' else 9
    entry = '西藏桃花節＋珠峰 11日' if days == 11 else '西藏桃花節不上珠峰 9日'
    result['selection_aliases'] = list(dict.fromkeys([*result.get('selection_aliases', []), entry]))
    text = quotation(days, 12180 if days == 11 else 10780, days == 11)
    # A historical migration must not downgrade a later customer-specific offer.
    rolling = next((s for s in result.get('fixed_answers', [])
                    if s['id'] == 'price' and s.get('valid_for_days')), None)
    if rolling:
        text = rolling['answer_text']
    for script in result.get('fixed_answers', []):
        if script['id'] == 'price':
            if rolling:
                continue
            script.update(answer_text=text, usage_note='业务2026-10-11报价模板。客户问费用才发送；不主动附加房差或独团加价。')
            if days == 11:
                script.update(valid_from='2026-10-11', valid_until='2026-10-31',
                              fallback_text=quotation(11, 12780))
        elif script['id'] in ('contact_request', 'contact_after_read'):
            script['answer_text'] = CONTACT
    for key in ('price_reference', 'price_deferral'):
        if key in result['content_groups']:
            result['content_groups'][key].update(approved_text=text, initial_delivery=False,
                purpose='客户明确问费用时使用当前有效price模板，不额外展开收费差异。')
    return result


def update_config(config):
    result = deepcopy(config)
    replacements = {
        'contact_family': '您先和家人討論～我們也可以接著協助您們確認出發安排。方便傳一下您的 LINE QR Code 給我嗎？',
        'contact_other_dates': '我們也有其他季節的西藏行程，以及中國其他地方的旅遊安排，可以依您的時間和喜好協助確認。方便傳一下您的 LINE QR Code 給我嗎？',
        'contact_email': '可以呀，留 Email 就好，我們可以用郵件和您聯絡。方便提供您的信箱嗎？',
        'contact_email_alternative': '如果用 Email 聯絡方便些，也可以留信箱，我們用郵件和您聯絡。',
    }
    for script in result.get('common_scripts', []):
        if script['id'] in replacements:
            script['text'] = replacements[script['id']]
    return result


def main():
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.route_packages import load_route_packages, install_route_package
    from app.db import SessionLocal
    from app.operations import setting_value, save_setting
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    packages = load_route_packages()
    with SessionLocal() as db:
        config = setting_value(db, 'route_reception_config', {})
        if args.apply:
            args.backup.mkdir(parents=True, exist_ok=False)
            (args.backup/'configuration.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf8')
            for rid, package in packages.items():
                (args.backup/(rid+'.json')).write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding='utf8')
                updated = update_package(package)
                if updated != package:
                    updated['package_version'] += '.business-20261011'
                    install_route_package(updated)
            save_setting(db, 'route_reception_config', update_config(config))
            db.commit()
    print(json.dumps({'applied': args.apply, 'promotion_until': '2026-10-31', 'nine_day_price_unchanged': True, 'messages_sent': 0}))


if __name__ == '__main__':
    main()
