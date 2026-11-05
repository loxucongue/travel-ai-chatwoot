"""Install the reviewed Ali delta inside a backed-up, quiesced deployment."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from app.main import app  # Register models.
from app.db import SessionLocal
from app.models import KnowledgeVersion, MaterialAsset, User
from app import route_packages as catalog
from app.reception_config import get_reception_configuration, put_reception_configuration, ReceptionConfiguration
from app.reception_v3.skills import compile_skills
from app.material_library import resolve_materials
from scripts.stage_tibet_routes_20261014 import install_materials
from scripts.configure_ali_series_20261102 import material_rules
from scripts.update_ali_north_central_20261105 import ALI, VERSION, update_package

ROOT=Path(__file__).resolve().parents[2]
RELEASE=ROOT/'data/knowledge/china2go/route-releases/20261105-ali-north-central'


def install(source, destination):
    manifest=json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    files={}
    for item in manifest:
        raw=(source/item['source_filename']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==item['sha256'] and len(raw)==item['byte_size']
        files[item['asset_key']]=raw
    catalog.ensure_route_packages_current()
    before_packages=deepcopy(catalog.ROUTE_PACKAGES)
    package=update_package(before_packages[ALI])
    catalog._validate(package,RELEASE/'route-package.json')
    with SessionLocal() as db:
        version=db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.tenant_id==1,
            KnowledgeVersion.version_key==catalog.KNOWLEDGE_VERSION))
        user=db.scalar(select(User).where(User.role=='super_admin',User.active.is_(True)).order_by(User.id))
        assert version and user
        for item in manifest:
            prior=db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id==version.id,
                MaterialAsset.asset_key==item['asset_key']))
            if prior:
                # Never revive an operator-revoked asset as a side effect of a content update.
                assert prior.available and prior.metadata_json.get('live_approved'), 'asset_revoked:'+item['asset_key']
                if item['asset_key']!='ali15_map':
                    assert prior.file_hash==item['sha256'],'unexpected_existing_asset:'+item['asset_key']
        install_materials(db,manifest,files,destination,1,user.id,version.id)
        for item in manifest:
            asset=db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id==version.id,
                MaterialAsset.asset_key==item['asset_key']))
            asset.metadata_json={**asset.metadata_json,'content_family':'asset:'+item['asset_key'],
                'what_it_shows':item['what_it_shows'],'customer_value':'了解阿里北線與中線的景點、住宿及行程',
                'avoid_claims':['圖片不代表已訂位；不保證天氣、野生動物或身體適應。']}
        db.flush()
        before=get_reception_configuration(db)
        configured=deepcopy(before)
        rules=material_rules({ALI:package});ids={r['id'] for r in rules}
        configured['material_rules']=[r for r in before['material_rules'] if r['id'] not in ids]+rules
        put_reception_configuration(db,ReceptionConfiguration.model_validate(configured))
        catalog.install_route_package(package)
        bundle=compile_skills(db)
        for item in manifest:
            resolved=resolve_materials(db,[item['asset_key']],ALI,1)[0]
            assert resolved['media_hash']==item['sha256']
        assert bundle['routes'][ALI]['large_party']['minimum']==6
        assert bundle['routes'][ALI]['profile_labels']==package['profile_labels']
        assert all(before[k]==configured[k] for k in before if k!='material_rules')
        assert all(catalog.ROUTE_PACKAGES[k]==v for k,v in before_packages.items() if k!=ALI)
        db.commit()
        return {'passed':True,'version':VERSION,'assets':len(manifest),'followup_topics':len(rules),
            'digest':bundle['digest'],'other_routes_preserved':True,'customer_test_sends':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--media-source',type=Path,required=True)
    parser.add_argument('--media-destination',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(install(args.media_source,args.media_destination),ensure_ascii=False))
