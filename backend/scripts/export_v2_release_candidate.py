"""Export an auditable code overlay and read-only local release preflight.

No database, secrets, customer history or environment files enter the archive.
This does not deploy or change approval/outbound settings.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile, ZIP_DEFLATED
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.models import MaterialAsset, WebKnowledgeSource, WebKnowledgeRevision
from app.reception_config import effective_reception_policy, default_reception_configuration, policy_from_configuration
from app.reception_v2 import ENGINE_RELEASE_ID, SKILL_RELEASE_DIGEST
from app.release_provenance import source_fingerprint
from app.route_packages import ROUTES


def digest(value):
    return hashlib.sha256(value).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    files=set()
    for folder, pattern in [('backend/app','*.py'),('backend/app/reception_v2/skills','SKILL.md'),
        ('backend/alembic','*.py'),('frontend/dist','*'),
        ('data/knowledge/china2go/route-packages','*.json'),('data/knowledge/china2go/service-knowledge','*.json'),
        ('data/knowledge/china2go/global-website-knowledge','*.json')]:
        files.update(p for p in (root/folder).rglob(pattern) if p.is_file())
    files.update((root/'data/knowledge/china2go/service-knowledge/sources').rglob('*.txt'))
    for name in ('backend/pyproject.toml','frontend/package.json','frontend/package-lock.json'):
        if (root/name).is_file():files.add(root/name)
    files.add(root/'backend/scripts/publish_reviewed_v2_content.py')
    required={key for spec in ROUTES.values() for group in spec['groups'].values() for key in group['assets']}
    assets=[]
    with SessionLocal() as db:
        policy=effective_reception_policy(db)
        service_revisions=[dict(row._mapping) for row in db.execute(select(
            WebKnowledgeSource.id.label('source_id'),WebKnowledgeSource.tenant_id,
            WebKnowledgeSource.ai_enabled,WebKnowledgeSource.runtime_scope,
            WebKnowledgeSource.published_revision_id,WebKnowledgeRevision.content_hash,
            WebKnowledgeRevision.status.label('revision_status')).outerjoin(WebKnowledgeRevision,
                WebKnowledgeRevision.id==WebKnowledgeSource.published_revision_id))]
        for asset in db.scalars(select(MaterialAsset).where(MaterialAsset.asset_key.in_(required))):
            meta=asset.metadata_json or {}
            path=Path(asset.source_path)
            actual=digest(path.read_bytes()) if path.is_file() else None
            assets.append({'key':asset.asset_key,'routes':meta.get('route_variants',[]),
                'approved':meta.get('live_approved') is True and meta.get('review_state')=='evaluation_ready',
                'available':asset.available,'expected_hash':asset.file_hash,'actual_hash':actual,
                'file_matches':bool(actual and actual==asset.file_hash),'media_type':asset.media_type})
        db.rollback()
    usable={a['key'] for a in assets if a['approved'] and a['available'] and a['file_matches']}
    manifest={'engine_release':ENGINE_RELEASE_ID,'skill_digest':SKILL_RELEASE_DIGEST,
        'source_fingerprint':source_fingerprint(),'model_alias':settings.deepseek_model,
        'generation':{'temperature':0.2,'thinking':'disabled','max_tokens':2200,'total_budget_seconds':30},
        'verification':{'parallel_scope_and_facts':True,'registered_fact_conditions_preserved':True,'task_proof_distinguishes_unknown_from_excluded':True,'current_question_overrides_historical_repetition':True,'connection_retry':{'maximum_per_node':1,'errors':['ConnectError','ConnectTimeout'],'shares_original_request_deadline':True},'initial_thinking':'disabled',
            'consultant_tasks':{'draft_independent_proof':True,'thinking':'enabled','reasoning_effort':'low','max_tokens':3000,'token_limit_fallback_tokens':1800,'reasoning_timeout_seconds':6,'fallback_on_reasoning_timeout':True,'proof_reuse':'same_turn_identical_request_facts_tasks'},
            'reply_shape_repair':{'max_tokens':600,'preserves_state':True,'fresh_audit_required':True},
            'semantic_revision':{'reasoning_effort':'low','full_max_tokens':4000,'copy_thinking':'disabled','copy_max_tokens':1800},
            'independent_request_reference':True,'max_draft_revisions':2,
            'rejected_facts_recheck':{'after_second_revision_only':True,'reasoning_effort':'low','max_tokens':3000,'reasoning_timeout_seconds':6,'fallback_tokens':1800},
            'scope_coverage_recheck':{'trigger':'unnecessary_question_or_repeat_content_or_covered_questions_with_missing_answer_or_event_disagreement','thinking':'disabled','max_tokens':1800},
            'final_deterministic_prune_only':True,'shares_turn_deadline':True,'tls_trust_context':'shared_process_wide_verified','connections':'thread_local'},
        'policy_digest':digest(json.dumps(policy,sort_keys=True,ensure_ascii=True).encode()),
        'service_knowledge_publications':service_revisions,
        'evaluated_default_matches_local_policy':policy==policy_from_configuration(default_reception_configuration()),
        'assets':sorted(assets,key=lambda a:a['key']),'missing_approved_assets':sorted(required-usable),
        'files':{p.relative_to(root).as_posix():digest(p.read_bytes()) for p in sorted(files)},
        'deployment_performed':False,'real_customer_messages':0,
        'scope':'Local source overlay and local asset/configuration observation; not a production-host receipt.'}
    args.output.mkdir(parents=True,exist_ok=True)
    with ZipFile(args.output/'v2-code-overlay.zip','w',ZIP_DEFLATED) as archive:
        for p in sorted(files):archive.write(p,p.relative_to(root).as_posix())
    manifest['archive_sha256']=digest((args.output/'v2-code-overlay.zip').read_bytes())
    (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'release':ENGINE_RELEASE_ID,'files':len(files),
        'missing_approved_assets':manifest['missing_approved_assets'],
        'policy_matches_test':manifest['evaluated_default_matches_local_policy'],'outbound':False}))


if __name__=='__main__':main()
