"""Publish only eight hot-loaded skills; preserve all routes, media and jobs."""
import os,sys,json,hashlib,socket,subprocess,sqlite3,fcntl,shutil
from pathlib import Path
ROOT=Path('/opt/china2go-ai');HERE=Path(__file__).resolve().parent;BACK=ROOT/'backups/tibet-entry-value-20261125';E=ROOT/'releases/tibet-entry-evidence-20261125'
manifest=json.loads((HERE/'manifest.json').read_text());mode=sys.argv[1];expected=sys.argv[2]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def query(sql):
 with sqlite3.connect('file:'+str(ROOT/'backend/data/app.db')+'?mode=ro',uri=True) as db:
  db.row_factory=sqlite3.Row;return [dict(x) for x in db.execute(sql)]
def inventory():
 return {'files':{str(p):sha(p) for base in ['backend/app','backend/data/route-packages','data/knowledge/china2go','frontend/dist'] for p in (ROOT/base).rglob('*') if p.is_file() and '__pycache__' not in str(p)},'processes':subprocess.check_output(['systemctl','show','china2go-api','china2go-worker','china2go-playground','-p','MainPID','-p','ExecMainStartTimestamp'],text=True),'nginx':subprocess.check_output(['nginx','-T'],stderr=subprocess.DEVNULL,text=True),'ingress':Path('/etc/cloudflared/config.yml').read_text(),'settings':query('select key,value from app_settings'),'assets':query('select * from material_assets'),'schema':query('select version_num from alembic_version')}
def atomic(p,data):
 tmp=p.with_suffix('.entry.tmp');tmp.write_bytes(data);os.replace(tmp,p)
assert socket.gethostname()=='iZt4n729nx7wezpcx0v6z1Z' and ROOT.resolve()==ROOT
commit=subprocess.check_output(['git','-C',str(HERE),'rev-parse','HEAD'],text=True).strip();assert commit==expected
assert not subprocess.check_output(['git','-C',str(HERE),'status','--porcelain','--untracked-files=no'],text=True).strip()
assert len(manifest)==8 and all(sha(HERE/m['payload'])==m['after_sha256'] for m in manifest)
assert shutil.disk_usage(ROOT).free>1024**3
assert int(next(s.split()[1] for s in Path('/proc/meminfo').read_text().splitlines() if s.startswith('MemAvailable:')))>512*1024
with (ROOT/'deploy.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 if mode=='rollback':
  for m in manifest:
   assert sha(ROOT/m['target'])==m['after_sha256'],'live file changed after release'
  for m in manifest:atomic(ROOT/m['target'],(BACK/m['target']).read_bytes())
  print(json.dumps({'status':'rolled_back','customer_data_unchanged':True}));sys.exit()
 assert mode in ['preflight','apply']
 assert all(sha(ROOT/m['target'])==m['before_sha256'] for m in manifest),'live baseline drift'
 before=inventory();assert '127.0.0.1:8080' in before['nginx'] and 'api.luoxuecong.asia' in before['ingress']
 routes=json.loads(next(s['value'] for s in before['settings'] if s['key']=='route_reception_config'))['routing']['enabled_route_variants'];assert len(routes)==9 and 'yunnan_tw_8d' in routes
 if mode=='preflight':print(json.dumps({'status':'preflight_passed','commit':commit,'routes':routes,'protected_files':len(before['files'])}));sys.exit()
 accepted=json.loads((E/'accepted.json').read_text());assert accepted['status']=='passed' and accepted['manifest_sha256']==sha(HERE/'manifest.json')
 assert not BACK.exists();BACK.mkdir(mode=0o700,parents=True)
 (BACK/'inventory.json').write_text(json.dumps(before));os.chmod(BACK/'inventory.json',0o600)
 for m in manifest:
  p=BACK/m['target'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/m['target']).read_bytes())
 assert before['processes']==inventory()['processes']
 try:
  for m in manifest:atomic(ROOT/m['target'],(HERE/m['payload']).read_bytes())
  after=inventory();targets={str(ROOT/m['target']):m['after_sha256'] for m in manifest}
  assert all(after['files'].get(p)==targets.get(p,h) for p,h in before['files'].items())
  assert all(before[k]==after[k] for k in ['processes','nginx','ingress','settings','assets','schema'])
  result={'status':'switched','commit':commit,'changed_skills':8,'restart':False,'protected_files':len(before['files']),'public_verification_pending':True};(BACK/'result.json').write_text(json.dumps(result));print(json.dumps(result))
 except Exception:
  for m in manifest:atomic(ROOT/m['target'],(BACK/m['target']).read_bytes())
  raise
