"""Apply the approved entry and six-hour timing settings without replaying sessions.

Run from backend: python scripts/update_intake_followup.py --apply
Rollback only these fields: python scripts/update_intake_followup.py --restore <backup>
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal
from app.operations import save_setting
from app.reception_config import IntakeSettings, SilenceSettings, ReceptionConfiguration, SETTING_KEY, get_reception_configuration


def updated(config):
    result = deepcopy(config)
    result['intake'] = IntakeSettings().model_dump()
    result['silence']['intervals_minutes'] = SilenceSettings().intervals_minutes
    ReceptionConfiguration.model_validate(result)
    return result


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--restore', type=Path)
    args = parser.parse_args()
    with SessionLocal() as db:
        before = get_reception_configuration(db)
        after = updated(before)
        backup = None
        if args.restore:
            saved = json.loads(args.restore.read_text(encoding='utf8'))
            after = deepcopy(before)
            # Do not overwrite unrelated edits made since deployment.
            if before['intake'] != saved['after']['intake'] or before['silence']['intervals_minutes'] != saved['after']['silence']['intervals_minutes']:
                raise RuntimeError('timing_settings_changed_since_migration')
            after['intake'] = saved['before']['intake']
            after['silence']['intervals_minutes'] = saved['before']['silence']['intervals_minutes']
        if args.apply and before != after:
            folder = Path('data/config-backups')
            folder.mkdir(parents=True, exist_ok=True)
            backup = folder / ('intake-followup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
            backup.write_text(json.dumps({'before': before, 'after': after}, ensure_ascii=False, indent=2), encoding='utf8')
        if args.apply or args.restore:
            save_setting(db, SETTING_KEY, after)
            db.commit()
        print(json.dumps({'applied': bool(args.apply or args.restore), 'backup': str(backup) if backup else None,
            'intake': after['intake'], 'silence': after['silence'],
            'opening_unchanged': before['reply']==after['reply'], 'sessions_replayed': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
