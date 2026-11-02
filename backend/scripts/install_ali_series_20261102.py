"""Install the approved Ali release. Run only inside a backed-up deployment."""
import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from app.db import SessionLocal
from app.main import app  # Register all models before resolving relationships.
from app import route_packages as catalog
from app.material_library import resolve_materials
from app.reception_config import get_reception_configuration, put_reception_configuration, ReceptionConfiguration
from app.reception_v3.skills import compile_skills
from scripts.configure_ali_series_20261102 import ALI,RELEASE,update_package,material_rules,install_assets


def install(media_source,media_destination):
    manifest=json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    files={}
    for spec in manifest:
        path=media_source/(spec['sha256']+Path(spec['source_filename']).suffix.lower())
        raw=path.read_bytes()
        if len(raw)!=spec['byte_size'] or hashlib.sha256(raw).hexdigest()!=spec['sha256']:
            raise ValueError('release_media_verification_failed')
        files[spec['asset_key']]=raw
    # Resolve existing packages afresh; do not replace unrelated operator edits.
    packages={key:update_package(value) for key,value in catalog.ROUTE_PACKAGES.items()}
    packages[ALI]=update_package(json.loads((RELEASE/'route-package.json').read_text(encoding='utf8')))
    for package in packages.values():catalog.install_route_package(package)
    with SessionLocal() as db:
        before=get_reception_configuration(db)
        install_assets(db,manifest,files,media_destination)
        configured=json.loads(json.dumps(before))
        configured['routing']['enabled_route_variants']=list(dict.fromkeys([*before['routing']['enabled_route_variants'],ALI]))
        rules=material_rules(packages);ids={r['id'] for r in rules}
        configured['material_rules']=[r for r in before['material_rules'] if r['id'] not in ids]+rules
        put_reception_configuration(db,ReceptionConfiguration.model_validate(configured))
        bundle=compile_skills(db)
        resolved=0
        for spec in manifest:
            for route in spec['route_variants']:
                item=resolve_materials(db,[spec['asset_key']],route,1)[0]
                assert item['media_hash']==spec['sha256']
                resolved+=1
        assert ALI in bundle['routes']
        for key in before:
            if key not in ('routing','material_rules'):assert before[key]==configured[key],key
        assert {k:v for k,v in before['routing'].items() if k!='enabled_route_variants'}=={
            k:v for k,v in configured['routing'].items() if k!='enabled_route_variants'}
        db.commit()
        return {'routes':list(bundle['routes']),'asset_count':len(manifest),'verified_bindings':resolved,
                'material_rules_added':len(rules),'digest':bundle['digest'],
                'enabled_route_added':ALI,'other_reception_settings_preserved':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--media-source',type=Path,required=True)
    parser.add_argument('--media-destination',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(install(args.media_source,args.media_destination),ensure_ascii=False))
