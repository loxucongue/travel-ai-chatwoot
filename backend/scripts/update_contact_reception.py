"""Apply the reviewed contact-reception configuration once, with a backup."""
from copy import deepcopy
import json
from pathlib import Path
import sys


SCENARIOS = [
    ('contact_family', '和家人商量时首次邀请',
     '介绍或主要问题已答；客户明确说要和家人商量；此前未邀请且未拒绝。只是日期未定时另用普通首次邀请，不假设要问家人。不是每次说考虑都重发。',
     '您先和家人討論，時間還不用急著決定。方便留一下您的 LINE ID 嗎？之後有想調整的地方，可以請顧問接著協助您。'),
    ('contact_other_dates', '其他月份的顾问承接',
     '客户询问现有线路以外的月份，先说明实际产品日期；此前未邀请且未拒绝。把九月替换为客户实际询问月份，不保证有团。',
     '這個月份的安排需要另外確認。方便留一下您的 LINE ID 嗎？我請顧問協助您看適合的行程。'),
    ('contact_email', '承接 Email 偏好',
     '客户主动指定 Email、寄信或邮箱，但尚未给地址；不能在客户已给信箱后再次索取。',
     '可以呀，留 Email 就好。方便提供您的信箱嗎？我請顧問用郵件和您聯絡。'),
    ('contact_email_alternative', '不便加好友但愿意邮件联系',
     '客户说明不方便用 LINE 或加好友，但仍愿意通过其他渠道联系；拒绝一切留资、只想在这里咨询或仅未回应时不使用。',
     '如果用 Email 聯絡方便些，也可以留信箱，我請顧問用郵件和您聯絡。'),
]


def updated_configuration(config):
    result = deepcopy(config)
    result['routing']['outside_catalog_action'] = 'consult_advisor'
    result['lead_capture']['require_supported_route'] = False
    # Remove only the obsolete, known test rule. Preserve other authored advice.
    obsolete = '线路产品不匹配转人工：客户想了解的项目没有匹配到的时候；客户想了解的项目没有匹配到的时候，直接转人工'
    result['reply']['custom_guidance'] = '\n'.join(
        line for line in result['reply'].get('custom_guidance', '').splitlines()
        if line.strip() != obsolete)
    scripts = result.setdefault('common_scripts', [])
    existing = {s['id'] for s in scripts}
    for key, name, scenario, text in SCENARIOS:
        if key not in existing:
            scripts.append(dict(id=key, name=name, scenario=scenario, text=text, enabled=True))
    return result


def main():
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.db import SessionLocal
    from app.reception_config import get_reception_configuration, ReceptionConfiguration, put_reception_configuration
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    with SessionLocal() as db:
        before = get_reception_configuration(db)
        after = ReceptionConfiguration.model_validate(updated_configuration(before))
        changed = [key for key in before if before[key] != after.model_dump().get(key)]
        if args.apply and changed:
            if args.backup is None:
                parser.error('--apply requires --backup')
            args.backup.parent.mkdir(parents=True, exist_ok=True)
            with args.backup.open('x', encoding='utf8') as file:
                json.dump(before, file, ensure_ascii=False, indent=2)
            put_reception_configuration(db, after)
            db.commit()
        print(json.dumps({'applied': args.apply, 'changed_sections': changed}, ensure_ascii=False))


if __name__ == '__main__':
    main()
