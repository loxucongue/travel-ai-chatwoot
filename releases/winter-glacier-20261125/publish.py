"""Narrow data hot-publish from a pinned repository checkout; never restart services."""
import contextlib,fcntl,hashlib,json,os,shutil,socket,sqlite3,subprocess,sys,time
from pathlib import Path
ROOT=Path('/opt/china2go-ai'); HERE=Path(__file__).resolve().parent
M=json.loads((HERE/'manifest.json').read_text()); MODE=sys.argv[1]
PKG=ROOT/'backend/data/route-packages/tibet_winter_8d/route-package.json'
SKILL=ROOT/'backend/app/reception_v3/skills/tibet-winter-8d/SKILL.md'
BACK=ROOT/'backups/winter-glacier-20261125'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def query(sql,args=()):
 with sqlite3.connect('file:'+str(ROOT/'backend/data/app.db')+'?mode=ro',uri=True) as d:
  d.row_factory=sqlite3.Row;return [dict(x) for x in d.execute(sql,args)]
def inv():
 paths=[p for base in ['backend/app','backend/data/route-packages','frontend/dist'] for p in (ROOT/base).rglob('*') if p.is_file() and '__pycache__' not in str(p)]
 return {'files':{str(p):sha(p) for p in paths},'processes':subprocess.check_output(['systemctl','show','china2go-api','china2go-worker','china2go-playground','-p','MainPID','-p','ExecMainStartTimestamp'],text=True),'nginx':subprocess.check_output(['nginx','-T'],stderr=subprocess.DEVNULL,text=True),'ingress':Path('/etc/cloudflared/config.yml').read_text(),'settings':query('select key,value from app_settings'),'assets':query('select * from material_assets'),'db_version':query('select version_num from alembic_version')}
def atomic(p,data):
 tmp=p.with_suffix('.glacier.tmp');tmp.write_bytes(data);os.replace(tmp,p)
assert socket.gethostname()==M['hostname'] and str(ROOT.resolve())==M['root']
assert sha(HERE/'route-package.json')==M['package_after'] and sha(HERE/'SKILL.md')==M['skill_after']
os.chdir(ROOT/'backend');sys.path.insert(0,str(ROOT/'backend'))
os.environ.update(APP_PROFILE='evaluation',OUTBOUND_MODE='disabled',CHATWOOT_WRITE_ENABLED='false')
from app import route_packages as rp
from app.reception_v3.skills import compile_skills,SkillRegistry
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
candidate=json.loads((HERE/'route-package.json').read_text());rp._validate(candidate,HERE/'route-package.json')
old=json.loads(PKG.read_text())
assert candidate['seasonal_pricing']==old['seasonal_pricing']
assert candidate['content_sequence']==old['content_sequence']
assert candidate['fixed_answers']==old['fixed_answers']
for k,g in old['content_groups'].items():
 if k not in ('itinerary','laigu'):assert g==candidate['content_groups'][k],k
assert all(not candidate['content_groups'][k]['asset_keys'] for k in ['itinerary','laigu'])
if MODE=='preflight':
 assert sha(PKG)==M['package_before'] and sha(SKILL)==M['skill_before']
 assert shutil.disk_usage(ROOT).free>1024**3
 print(json.dumps({'preflight':'passed','hot_reload':hasattr(rp,'ensure_route_packages_current'),'changes':['winter package','winter skill','disable old map and Laigu photo'],'restart':False}));sys.exit()
with (ROOT/'deploy.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 if MODE=='rollback':
  before=json.loads((BACK/'inventory.json').read_text())
  assert sha(PKG)==M['package_after'] and sha(SKILL)==M['skill_after'],'changed since release'
  atomic(PKG,(BACK/'route-package.json').read_bytes());atomic(SKILL,(BACK/'SKILL.md').read_bytes())
  with sqlite3.connect(ROOT/'backend/data/app.db') as d:
   for a in M['disabled_assets']:d.execute('update material_assets set available=? where id=? and file_hash=?',(a['available'],a['id'],a['file_hash']))
  print(json.dumps({'rollback':'applied','restart':False}));sys.exit()
 assert MODE=='apply'
 assert sha(PKG)==M['package_before'] and sha(SKILL)==M['skill_before'],'live drift'
 assert not BACK.exists(),'release already exists'
 BACK.mkdir(mode=0o700,parents=True)
 before=inv();(BACK/'inventory.json').write_text(json.dumps(before,ensure_ascii=False));os.chmod(BACK/'inventory.json',0o600)
 shutil.copy2(PKG,BACK/'route-package.json');shutil.copy2(SKILL,BACK/'SKILL.md')
 commit=subprocess.check_output(['git','-C',str(HERE),'rev-parse','HEAD'],text=True).strip()
 try:
  with sqlite3.connect(ROOT/'backend/data/app.db') as d:
   d.execute('begin immediate')
   for a in M['disabled_assets']:
    row=d.execute('select asset_key,file_hash,available from material_assets where id=?',(a['id'],)).fetchone()
    assert row==(a['asset_key'],a['file_hash'],a['available']),row
   atomic(PKG,(HERE/'route-package.json').read_bytes());atomic(SKILL,(HERE/'SKILL.md').read_bytes())
   for a in M['disabled_assets']:d.execute('update material_assets set available=0 where id=?',(a['id'],))
  rp.ensure_route_packages_current()
  with Session(create_engine('sqlite:///file:'+str(ROOT/'backend/data/app.db')+'?mode=ro&uri=true')) as db:
   bundle=compile_skills(db);r=bundle['routes']['tibet_winter_8d']
   assert '仁龍巴' in r['groups']['laigu']['text'] and not r['groups']['laigu']['assets']
   assert '仁龍巴' in SkillRegistry(bundle).load('tibet-winter-8d')['instructions']
   from app.material_library import candidate_materials
   tenants=query('select distinct tenant_id from knowledge_versions')
   for t in tenants:
    assert not {a['asset_key'] for a in M['disabled_assets']} & {a['key'] for a in candidate_materials(db,t['tenant_id'])}
  after=inv()
  assert before['processes']==after['processes']
  assert before['nginx']==after['nginx'] and before['ingress']==after['ingress']
  assert before['settings']==after['settings'] and before['db_version']==after['db_version']
  for path,h in before['files'].items():
   if path not in [str(PKG),str(SKILL)]:assert after['files'].get(path)==h,path
  for a,b in zip(before['assets'],after['assets']):
   if a['id'] in [x['id'] for x in M['disabled_assets']]:a=dict(a,available=0)
   assert a==b,'unexpected material change'
  report={'commit':commit,'status':'applied','package_hash':sha(PKG),'skill_hash':sha(SKILL),'all_routes_compiled':list(bundle['routes']),'unchanged_processes':after['processes'],'protected_files_checked':len(before['files']),'disabled_assets':M['disabled_assets']}
  (BACK/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
 except Exception:
  atomic(PKG,(BACK/'route-package.json').read_bytes());atomic(SKILL,(BACK/'SKILL.md').read_bytes())
  with sqlite3.connect(ROOT/'backend/data/app.db') as d:
   for a in M['disabled_assets']:d.execute('update material_assets set available=? where id=?',(a['available'],a['id']))
  raise
