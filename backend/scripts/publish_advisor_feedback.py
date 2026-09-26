"""Publish new SOP snapshots without resetting operator controls or active jobs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.automation_service import sop_snapshot
from app.db import SessionLocal
from app.models import KnowledgeVersion, MaterialAsset, OutboundMessage, SopDefinition, User
from app.outbound_control import global_message_sending_enabled
from app.reception_config import effective_reception_policy
from app.route_packages import ROUTE_PACKAGES, _runtime_journey_sop
from setup_routes_1_2 import _sop_nodes, backup_database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with SessionLocal() as db:
        if global_message_sending_enabled(db):
            raise SystemExit('global_message_sending_must_be_disabled')
        before = db.scalar(select(func.count(OutboundMessage.id)))
        planned = []
        for route, package in ROUTE_PACKAGES.items():
            rows = db.scalars(select(SopDefinition).where(
                SopDefinition.route_variant == route, SopDefinition.status == 'running',
                SopDefinition.name == package['runtime_sop']['name'],
            )).all()
            if len(rows) != 1:
                raise SystemExit(f'ambiguous_route_sop:{route}:{len(rows)}')
            sop = rows[0]
            assets = {a.asset_key: a for a in db.scalars(select(MaterialAsset).join(KnowledgeVersion).where(
                KnowledgeVersion.tenant_id == sop.tenant_id,
            ).order_by(MaterialAsset.id)).all()}
            compiled = {**package, 'runtime_sop': _runtime_journey_sop(package, effective_reception_policy(db))}
            nodes = _sop_nodes(compiled, assets)
            planned.append((sop, nodes, package['package_version']))
        backup = backup_database() if args.apply else None
        report = []
        for sop, nodes, version in planned:
            changed = sop.nodes != nodes
            report.append({'sop_id': sop.id, 'route': sop.route_variant, 'package_version': version,
                           'changed': changed, 'old_version': sop.version})
            if args.apply and changed:
                owner = db.scalar(select(User).where(User.role.in_(['admin', 'super_admin']), User.active.is_(True)).order_by(User.id))
                if owner is None:
                    raise SystemExit('administrator_missing')
                sop.nodes = nodes
                sop.version += 1
                sop_snapshot(db, sop, owner.id)
        if args.apply:
            db.commit()
        after = db.scalar(select(func.count(OutboundMessage.id)))
        assert before == after
        print(json.dumps({'applied': args.apply, 'backup': backup, 'routes': report,
                          'outbound_before': before, 'outbound_after': after}, ensure_ascii=False))


if __name__ == '__main__':
    main()
