"""Rehearse route installation in a separate local database; never touch live data."""
import argparse
from copy import deepcopy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / 'data/knowledge/china2go/route-releases/20261014'


def verify_archives(archive_dir):
    manifest = json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    files = {}
    for archive in sorted({m['source_archive'] for m in manifest}):
        with zipfile.ZipFile(archive_dir/archive) as z:
            members = {}
            for info in z.infolist():
                if info.is_dir(): continue
                name = info.filename if info.flag_bits & 0x800 else info.filename.encode('cp437').decode('big5')
                members[Path(name).name] = info
            for asset in [m for m in manifest if m['source_archive']==archive]:
                data = z.read(members[asset['source_filename']])
                if hashlib.sha256(data).hexdigest() != asset['sha256'] or len(data) != asset['byte_size']:
                    raise ValueError('release_asset_hash_mismatch:'+asset['asset_key'])
                files[asset['asset_key']] = data
    return manifest, files


def install_materials(db, manifest, files, media_dir, tenant_id, user_id, version_id, first_media_id=None):
    from sqlalchemy import select
    from app.models import MaterialAsset, StoredMedia
    media_dir.mkdir(parents=True, exist_ok=True)
    for index,asset in enumerate(manifest):
        path = media_dir/(asset['sha256']+Path(asset['source_filename']).suffix.lower())
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            raise ValueError('staging_existing_media_changed')
        path.write_bytes(files[asset['asset_key']])
        media = db.scalar(select(StoredMedia).where(StoredMedia.tenant_id==tenant_id, StoredMedia.storage_path==str(path)))
        if not media:
            media = StoredMedia(tenant_id=tenant_id, created_by=user_id, original_name=asset['source_filename'],
                media_type='image', mime_type=mimetypes.guess_type(path.name)[0], file_size=len(files[asset['asset_key']]), storage_path=str(path))
            if first_media_id is not None: media.id=first_media_id+index
            db.add(media); db.flush()
        item = db.scalar(select(MaterialAsset).where(MaterialAsset.knowledge_version_id==version_id, MaterialAsset.asset_key==asset['asset_key']))
        if not item:
            item = MaterialAsset(knowledge_version_id=version_id, asset_key=asset['asset_key'])
            db.add(item)
        item.source_path=str(path); item.display_name=asset['display_name']; item.media_type='image'
        item.file_hash=asset['sha256']; item.available=True; item.usage=asset['content_group_key']
        item.metadata_json={**asset, 'stored_media_id':media.id,'route_variants':[asset['route_variant']],
                            'review_state':'approved','live_approved':True}
    db.flush()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--archives', type=Path, required=True)
    p.add_argument('--snapshot', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args=p.parse_args(); output=args.output.resolve()
    if not output.is_relative_to(ROOT/'output'):
        raise ValueError('staging_output_must_be_inside_workspace_output')
    output.mkdir(parents=True,exist_ok=True)
    db_path=output/'staging.db'
    if db_path.exists(): raise ValueError('staging_database_exists_choose_new_output')
    manifest,files=verify_archives(args.archives)
    source=json.loads(args.snapshot.read_text(encoding='utf8'))
    os.environ.update(APP_PROFILE='evaluation',DATABASE_URL='sqlite:///'+db_path.as_posix(),
        OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false',RELAY_BASE_URL='',RELAY_API_TOKEN='')
    sys.path.insert(0,str(ROOT/'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.main import app
    from app.db import Base
    from app.models import Tenant,User,KnowledgeVersion,StoredMedia
    import app.route_packages as catalog
    from app.reception_config import ReceptionConfiguration,put_reception_configuration
    from app.reception_v3.skills import compile_skills,SkillRegistry
    from app.material_library import candidate_materials,resolve_materials
    catalog.RUNTIME_PACKAGE_ROOT=output/'runtime-packages'
    from scripts.configure_seasonal_retention_20261014 import update_package as update_availability
    for package in source['packages'].values():
        catalog.install_route_package(update_availability(package))
    for path in RELEASE.glob('*/route-package.json'):
        catalog.install_route_package(json.loads(path.read_text(encoding='utf8')))
    engine=create_engine('sqlite:///'+db_path.as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1,name='isolated-tibet-release'))
        db.add(User(id=1,email='staging@example.com',display_name='Staging',password_hash='unused',role='super_admin'))
        db.add(KnowledgeVersion(id=1,tenant_id=1,version_key=catalog.KNOWLEDGE_VERSION,title='Isolated release',content_hash='staging',source_summary={}))
        db.flush()
        for key,info in source['snapshot']['opening_media'].items():
            saved=json.loads((args.snapshot.parent/f'opening-{key}.json').read_text(encoding='utf8'))
            image=Path(saved['storage_path'])
            assert hashlib.sha256(image.read_bytes()).hexdigest()==info['media_hash']
            db.add(StoredMedia(**{**saved,'tenant_id':1,'created_by':1}))
        install_materials(db,manifest,files,output/'media',1,1,1,first_media_id=1000000)
        config=deepcopy(source['raw_config'])
        config['routing']['enabled_route_variants']=list(catalog.ROUTE_PACKAGES)
        put_reception_configuration(db,ReceptionConfiguration.model_validate(config))
        db.commit()
        bundle=compile_skills(db)
        for route in ('peach_9d_2027', 'peach_11d_2027'):
            assert bundle['routes'][route]['departure_availability']['years'] == [2027]
        materials=candidate_materials(db,1)
        resolved=[]
        for asset in manifest:
            info=resolve_materials(db,[asset['asset_key']],asset['route_variant'],1)[0]
            assert info['media_hash']==asset['sha256']
            resolved.append(info)
        registry=SkillRegistry(bundle,materials)
        assert len(registry.index())==5
        for route in bundle['routes']:
            assert registry.route_skill(route)
        snapshot={**source['snapshot'], 'bundle':bundle, 'materials':source['snapshot']['materials']+materials}
        for filename,data in [('snapshot.json',snapshot),('config.json',config),('materials.json',resolved)]:
            (output/filename).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
        result={'isolated':True,'outbound':False,'database':str(db_path),'routes':list(bundle['routes']),
            'assets_resolved':len(resolved),'unique_images':len({m['media_hash'] for m in resolved}),
            'dated_routes': [k for k,v in bundle['routes'].items() if v.get('seasonal_pricing') or v.get('departure_availability')],
            'production_changed':False}
        (output/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result))


if __name__=='__main__':main()
