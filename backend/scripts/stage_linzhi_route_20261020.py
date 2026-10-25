"""Build an isolated local acceptance DB. Never connects to production or a model."""
import argparse
from copy import deepcopy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT/'data/knowledge/china2go/route-releases/20261020-linzhi8'
ROUTE = 'tibet_linzhi_8d'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--config-source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT/'output').resolve()):
        raise ValueError('acceptance_output_must_be_local_workspace_output')
    output.mkdir(parents=True,exist_ok=True)
    db_path = output/'staging.db'
    if db_path.exists():
        raise ValueError('acceptance_database_exists_use_new_output')
    os.environ.update(APP_PROFILE='evaluation',DATABASE_URL='sqlite:///'+db_path.as_posix(),
        OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',
        RELAY_BASE_URL='',RELAY_API_TOKEN='',CUSTOMER_IMAGE_VISION_ENABLED='false')
    sys.path.insert(0,str(ROOT/'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.main import app  # Register models; no server/startup.
    from app.db import Base
    from app.models import Tenant,User,KnowledgeVersion,MaterialAsset,StoredMedia
    import app.route_packages as catalog
    from app.reception_config import ReceptionConfiguration,put_reception_configuration
    from app.reception_v3.skills import compile_skills,SkillRegistry
    from app.material_library import candidate_materials,resolve_materials
    from app.reception_v3.followup_window import FOLLOWUP_WINDOW
    baseline = json.loads(args.baseline.read_text(encoding='utf8'))
    source = json.loads(args.config_source.read_text(encoding='utf8'))
    package = json.loads((RELEASE/'tibet-linzhi-8d/route-package.json').read_text(encoding='utf8'))
    catalog.RUNTIME_PACKAGE_ROOT = output/'runtime-packages'
    for p in [*baseline['packages'].values(),package]:
        catalog.install_route_package(p)
    engine = create_engine('sqlite:///'+db_path.as_posix())
    Base.metadata.create_all(engine)
    manifest = json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    with Session(engine) as db:
        db.add(Tenant(id=1,name='Linzhi acceptance only'))
        db.add(User(id=1,email='acceptance@example.invalid',display_name='Acceptance',
                    password_hash='unused',role='super_admin'))
        db.add(KnowledgeVersion(id=1,tenant_id=1,version_key=catalog.KNOWLEDGE_VERSION,
            title='Linzhi acceptance only',content_hash='linzhi-acceptance',source_summary={}))
        db.flush()
        for a in manifest:
            path = (RELEASE/a['path']).resolve()
            assert path.is_relative_to(RELEASE.resolve())
            assert hashlib.sha256(path.read_bytes()).hexdigest() == a['sha256']
            media = StoredMedia(tenant_id=1,created_by=1,original_name=a['source_filename'],
                media_type='image',mime_type=mimetypes.guess_type(path.name)[0],
                file_size=a['byte_size'],storage_path=str(path))
            db.add(media);db.flush()
            db.add(MaterialAsset(knowledge_version_id=1,asset_key=a['asset_key'],
                source_path=str(path),display_name=a['display_name'],media_type='image',
                file_hash=a['sha256'],available=True,usage=a['content_group_key'],
                metadata_json={**a,'stored_media_id':media.id,'route_variants':[ROUTE],
                    'content_family':'asset:'+a['sha256'],'review_state':'pending','live_approved':False,
                    'acceptance_only':True}))
        config = deepcopy(source['config'])
        config['routing']['enabled_route_variants'] = list(catalog.ROUTES)
        # Opening attachments would require unrelated production media. This
        # product uses the already approved text-only greeting.
        config['reply']['opening_items'] = [{'key':'opening-0','content_type':'text',
            'content':'您好～我是China2Go的旅遊顧問～很高興認識你'}]
        config['reply']['opening_messages'] = ['您好～我是China2Go的旅遊顧問～很高興認識你']
        put_reception_configuration(db,ReceptionConfiguration.model_validate(config))
        db.commit()
        bundle = compile_skills(db)
        materials = candidate_materials(db,1)
        resolved = [resolve_materials(db,[a['asset_key']],ROUTE,1)[0] for a in manifest]
        assert len(resolved) == 18 and len({m['media_hash'] for m in resolved}) == 18
        registry = SkillRegistry(bundle,materials)
        assert registry.route_skill(ROUTE) == 'tibet-linzhi-8d'
        assert len(bundle['routes']) == 6 and FOLLOWUP_WINDOW.days == 7
        summary = {'status':'local_acceptance_only','database':str(db_path),
            'routes':list(bundle['routes']),'assets_resolved':len(resolved),'bundle_digest':bundle['digest'],
            'production_changed':False,'outbound_enabled':False,'live_approved_assets':0,
            'followup_days':FOLLOWUP_WINDOW.days,'requires_user_approval':True}
        snapshot = {'bundle':bundle,'materials':materials,'website_facts':[],
                    'website_version':'linzhi-source-20261020','opening_media':{}}
        for name,data in [('snapshot.json',snapshot),('summary.json',summary),('config.json',config)]:
            (output/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(summary,ensure_ascii=False))


if __name__ == '__main__':
    main()
