"""Publish the requested LINE wording to existing configuration and route overrides.

One-time authoring change, never a model-output replacement. Default is preview.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys

QR_INVITATION = '方便傳一下您的 LINE QR Code 給我嗎？'


def wording(text):
    for old in ('方便留一下您的LINE ID嗎？', '方便留一下您的 LINE ID 嗎？'):
        text = text.replace(old, QR_INVITATION)
    return text


def update_package(package):
    result = deepcopy(package)
    for group in result.get('content_groups', {}).values():
        group['approved_text'] = wording(group.get('approved_text', ''))
    for script in result.get('fixed_answers', []):
        script['answer_text'] = wording(script['answer_text'])
    return result


def update_configuration(config):
    result = deepcopy(config)
    for script in result.get('common_scripts', []):
        script['text'] = wording(script['text'])
    return result


def main():
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.route_packages import load_route_packages, install_route_package
    from app.db import SessionLocal
    from app.reception_config import get_reception_configuration, ReceptionConfiguration, put_reception_configuration
    from app.operations import audit
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if args.apply and args.backup is None:
        parser.error('--apply requires --backup')
    packages = []
    for route, before in list(load_route_packages().items()):
        after = update_package(before)
        if after != before:
            packages.append((route, before, after))
    with SessionLocal() as db:
        before = get_reception_configuration(db)
        after = ReceptionConfiguration.model_validate(update_configuration(before))
        changed = after.model_dump() != before
        if args.apply and (packages or changed):
            args.backup.mkdir(parents=True, exist_ok=False)
            (args.backup / 'reception-config.json').write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf8')
            for route, original, updated in packages:
                (args.backup / (route + '.json')).write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding='utf8')
                updated['package_version'] += '.line-qr-20261008'
                install_route_package(updated)
            if changed:
                put_reception_configuration(db, after)
            audit(db, None, 'configuration.line_qr_invitation', 'reception', 'line_qr',
                  {'routes': [r for r, _, _ in packages], 'common_scripts_updated': changed})
            db.commit()
    print(json.dumps({'applied': args.apply, 'routes': [r for r, _, _ in packages],
                      'common_scripts_changed': changed, 'messages_sent': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
