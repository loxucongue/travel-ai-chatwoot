"""Hot-install only the reviewed Yunnan namespace, from a clean Git checkout."""
import os,sys,json,hashlib,socket,subprocess,sqlite3,fcntl,shutil
from pathlib import Path
ROOT=Path('/opt/china2go-ai');HERE=Path(__file__).resolve().parent;R='yunnan_tw_8d'
BACK=ROOT/'backups/yunnan-tw-20261125';PKG=ROOT/f'backend/data/route-packages/{R}/route-package.json';SKILL=ROOT/'backend/app/reception_v3/skills/yunnan-tw-8d/SKILL.md'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def query(sql,args=()):
 with sqlite3.connect('file:'+str(ROOT/'backend/data/app.db')+'?mode=ro',uri=True) as d:
  d.row_factory=sqlite3.Row;return [dict(r) for r in d.execute(sql,args)]
def inventory():
 files={str(p):sha(p) for base in ['backend/app','backend/data/route-packages','data/knowledge/china2go','frontend/dist'] for p in (ROOT/base).rglob('*') if p.is_file() and '__pycache__' not in str(p)}
 return dict(files=files,processes=subprocess.check_output(['systemctl','show','china2go-api','china2go-worker','china2go-playground','-p','MainPID','-p','ExecMainStartTimestamp'],text=True),nginx=subprocess.check_output(['nginx','-T'],stderr=subprocess.DEVNULL,text=True),ingress=Path('/etc/cloudflared/config.yml').read_text(),settings=query('select key,value from app_settings'),assets=query('select * from material_assets'),schema=query('select version_num from alembic_version'))
def atomic(p,data):
 p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.yunnan.tmp');tmp.write_bytes(data);os.replace(tmp,p)
assert socket.gethostname()=='iZt4n729nx7wezpcx0v6z1Z' and str(ROOT.resolve())=='/opt/china2go-ai'
assert not subprocess.check_output(['git','-C',str(HERE),'status','--porcelain','--untracked-files=no'],text=True).strip(),'dirty release checkout'
commit=subprocess.check_output(['git','-C',str(HERE),'rev-parse','HEAD'],text=True).strip()
os.chdir(ROOT/'backend');sys.path.insert(0,str(ROOT/'backend'))
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false',LIVE_SOP_ENABLED='false')
from sqlalchemy import create_engine,select,text
from sqlalchemy.orm import Session
from app import route_packages as rp
from app.models import MaterialAsset,KnowledgeVersion,User
from app.reception_v3.skills import compile_skills,SkillRegistry
from app.material_library import candidate_materials,resolve_materials
from scripts.stage_tibet_routes_20261014 import install_materials
candidate=rp._validate(json.loads((HERE/'route-package.json').read_text()),HERE/'route-package.json')
manifest=json.loads((HERE/'asset-manifest.json').read_text());active=[a for a in manifest if a['available']]
assert candidate['route_variant']==R and len(active)==49 and len(manifest)==50
assert all(sha(HERE/'media'/(a['sha256']+'.jpg'))==a['sha256'] for a in manifest)
assert not any(R+'_yn_1' in g['asset_keys'] for g in candidate['content_groups'].values())
assert shutil.disk_usage(ROOT).free>1024**3
assert b'MemAvailable:' in Path('/proc/meminfo').read_bytes()
mode=sys.argv[1]
with (ROOT/'deploy.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 if mode=='rollback':
  saved=json.loads((BACK/'inventory.json').read_text());before_config=json.loads(next(s['value'] for s in saved['settings'] if s['key']=='route_reception_config'))
  with sqlite3.connect(ROOT/'backend/data/app.db') as d:
   d.execute('begin immediate');raw=d.execute('select value from app_settings where key=?',('route_reception_config',)).fetchone()[0];c=json.loads(raw)
   c['routing']['enabled_route_variants']=[k for k in c['routing']['enabled_route_variants'] if k!=R]
   d.execute('update app_settings set value=? where key=?',(json.dumps(c,ensure_ascii=False),'route_reception_config'))
   d.execute('update material_assets set available=0 where asset_key like ?', (R+'_%',))
  print(json.dumps({'rollback':'disabled only new route/assets; all customer data retained','commit':commit}));sys.exit()
 assert mode in ['preflight','apply']
 assert not PKG.exists() and not SKILL.exists() and R not in rp.load_route_packages(),'route already present'
 assert not query('select id from material_assets where asset_key like ?', (R+'_%',))
 before=inventory();raw=next(s['value'] for s in before['settings'] if s['key']=='route_reception_config');config=json.loads(raw)
 assert config['lead_capture']['post_quote_invitation_minutes']==5
 assert 'ai.luoxuecong.asia' in before['nginx'] and '127.0.0.1:8080' in before['nginx']
 assert 'api.luoxuecong.asia' in before['ingress'] and '127.0.0.1:8000' in before['ingress']
 assert len(config['routing']['enabled_route_variants'])==8
 with Session(create_engine('sqlite:///file:'+str(ROOT/'backend/data/app.db')+'?mode=ro&uri=true')) as db:old_bundle=compile_skills(db)
 if mode=='preflight':
  print(json.dumps({'preflight':'passed','commit':commit,'existing_routes':list(old_bundle['routes']),'new_assets':len(active),'restart':False,'protected_files':len(before['files'])}));sys.exit()
 assert not BACK.exists(),'release already attempted'
 BACK.mkdir(mode=0o700,parents=True);(BACK/'inventory.json').write_text(json.dumps(before,ensure_ascii=False));os.chmod(BACK/'inventory.json',0o600)
 changed=json.loads(raw);changed['routing']['enabled_route_variants'].append(R)
 committed=False
 try:
  # Disabled until the single DB transaction exposes all material rows plus routing.
  atomic(SKILL,(HERE/'SKILL.md').read_bytes());atomic(PKG,(HERE/'route-package.json').read_bytes());rp.reload_route_packages()
  with Session(create_engine('sqlite:///'+str(ROOT/'backend/data/app.db'))) as db:
   db.execute(text('BEGIN IMMEDIATE'))
   current=db.execute(text('select value from app_settings where key=:key'),{'key':'route_reception_config'}).scalar_one();assert current==raw,'configuration changed'
   assert before['processes']==inventory()['processes'],'running services changed'
   version=db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.tenant_id==1,KnowledgeVersion.version_key==candidate['knowledge_version']))
   user=db.scalar(select(User).where(User.role=='super_admin',User.active==True).order_by(User.id));assert version and user
   files={a['asset_key']:(HERE/'media'/(a['sha256']+'.jpg')).read_bytes() for a in active}
   install_materials(db,active,files,ROOT/'backend/data/uploads/yunnan-tw-20261125',1,user.id,version.id)
   db.execute(text('update app_settings set value=:value where key=:key'),{'value':json.dumps(changed,ensure_ascii=False),'key':'route_reception_config'})
   db.flush();bundle=compile_skills(db)
   assert set(bundle['routes'])==set(old_bundle['routes'])|{R}
   assert all(bundle['routes'][r]==old_bundle['routes'][r] for r in old_bundle['routes'])
   assert SkillRegistry(bundle).route_skill(R)=='yunnan-tw-8d'
   for a in active:
    resolved=resolve_materials(db,[a['asset_key']],R,1);assert resolved and resolved[0]['media_hash']==a['sha256']
   assert not any(m['key']==R+'_yn_1' for m in candidate_materials(db,1))
   db.commit();committed=True
  after=inventory()
  assert before['processes']==after['processes'] and before['nginx']==after['nginx'] and before['ingress']==after['ingress']
  assert before['schema']==after['schema']
  assert all(after['files'].get(p)==h for p,h in before['files'].items()),'protected file changed'
  after_assets={a['id']:a for a in after['assets']};assert all(after_assets.get(a['id'])==a for a in before['assets'])
  after_settings={s['key']:s['value'] for s in after['settings']}
  assert all(after_settings[s['key']]==s['value'] for s in before['settings'] if s['key']!='route_reception_config')
  assert json.loads(after_settings['route_reception_config'])==changed
  report={'status':'switched','commit':commit,'package_hash':sha(PKG),'skill_hash':sha(SKILL),'enabled_routes':list(bundle['routes']),'new_materials':49,'protected_files':len(before['files']),'restart':False,'public_verification_pending':True}
  (BACK/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report))
 except Exception:
  if committed:
   with sqlite3.connect(ROOT/'backend/data/app.db') as d:
    current=json.loads(d.execute('select value from app_settings where key=?',('route_reception_config',)).fetchone()[0]);current['routing']['enabled_route_variants']=[k for k in current['routing']['enabled_route_variants'] if k!=R]
    d.execute('update app_settings set value=? where key=?',(json.dumps(current,ensure_ascii=False),'route_reception_config'));d.execute('update material_assets set available=0 where asset_key like ?', (R+'_%',))
  else:
   if PKG.exists():PKG.unlink()
   if SKILL.exists():SKILL.unlink()
  raise
