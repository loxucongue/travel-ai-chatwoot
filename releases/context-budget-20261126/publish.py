"""Pinned, locked four-file release with prepared sequential worker handoffs."""
import fcntl,hashlib,json,os,shutil,socket,sqlite3,subprocess,sys,time
from pathlib import Path
ROOT=Path('/opt/china2go-ai');HERE=Path(__file__).resolve().parent
BACK=ROOT/'backups/context-budget-20261126'
M=json.loads((HERE/'manifest.json').read_text())
def cmd(*args):return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT).strip()
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def pid(unit):return int(cmd('systemctl','show',unit,'--value','-p','MainPID'))
def atomic(p,data):
    tmp=p.with_suffix('.context.tmp');tmp.write_bytes(data);os.replace(tmp,p)
def inventory():
    with sqlite3.connect('file:'+str(ROOT/'backend/data/app.db')+'?mode=ro',uri=True) as db:
        settings=db.execute('select key,value from app_settings').fetchall();assets=db.execute('select * from material_assets').fetchall();schema=db.execute('select version_num from alembic_version').fetchall()
    return {'files':{str(p):sha(p) for base in ['backend/app','backend/data/route-packages','data/knowledge/china2go','frontend/dist'] for p in (ROOT/base).rglob('*') if p.is_file() and '__pycache__' not in str(p)},'settings':settings,'assets':assets,'schema':schema,'nginx':cmd('nginx','-T'),'tunnel':Path('/etc/cloudflared/config.yml').read_text(),'api_pid':pid('china2go-api'),'units':{u:cmd('systemctl','cat',u) for u in ['china2go-api','china2go-worker','china2go-playground']},'pids':{u:pid(u) for u in ['china2go-worker','china2go-playground']}}
def record(value):
    (BACK/'progress.json').write_text(json.dumps(value));print(json.dumps(value),flush=True)
def ready(path,expected_pid,expected_hash,active=False):
    end=time.monotonic()+60
    while time.monotonic()<end:
        if path.exists():
            x=json.loads(path.read_text())
            if x['pid']==expected_pid and x['runtime_sha256']==expected_hash and (not active or x['state']=='active'):return x
        time.sleep(.2)
    raise RuntimeError('worker_readiness_timeout:'+str(path))
def handoff(unit,environment,expected_hash):
    old=pid(unit);assert old>0
    temporary=unit+'-context-standby'
    temp_ready=ROOT/('backend/data/'+temporary+'.json');final_ready=ROOT/('backend/data/'+unit+'-march.json')
    bootstrap=str(HERE/'worker_bootstrap.py');python=str(ROOT/'venv/bin/python')
    # Existing playground has no process lock. New standby imports and waits for its PID.
    wait=['--wait-pid',str(old)] if environment=='playground' else []
    cmd('systemd-run','--unit='+temporary,'--property=User=china2go','--property=Group=china2go','--property=WorkingDirectory='+str(ROOT/'backend'),'--property=KillSignal=SIGINT','--property=KillMode=mixed','--property=TimeoutStopSec=300','--property=SendSIGKILL=no','--property=SuccessExitStatus=SIGINT','--property=MemoryMax=900M',python,bootstrap,environment,'--ready',str(temp_ready),*wait)
    standby=pid(temporary);ready(temp_ready,standby,expected_hash)
    drop=Path('/etc/systemd/system')/(unit+'.service.d')/'90-march-handoff.conf';drop.parent.mkdir(exist_ok=True)
    previous=drop.read_bytes() if drop.exists() else None
    (BACK/(unit+'-previous-dropin.json')).write_text(json.dumps({'content':previous.decode() if previous else None}))
    atomic(drop,b'[Service]\nKillSignal=SIGINT\nKillMode=mixed\nTimeoutStopSec=300\nSendSIGKILL=no\nSuccessExitStatus=SIGINT\n')
    cmd('systemctl','daemon-reload')
    assert pid(unit)==old,'concurrent worker change'
    record({'phase':'draining_original','unit':unit,'old_pid':old,'standby_pid':standby})
    cmd('systemctl','stop',unit)
    assert not Path('/proc/'+str(old)).exists(),'old worker did not drain'
    ready(temp_ready,standby,expected_hash,True)
    atomic(drop,('[Service]\nKillSignal=SIGINT\nKillMode=mixed\nTimeoutStopSec=300\nSendSIGKILL=no\nSuccessExitStatus=SIGINT\nExecStart=\nExecStart='+python+' '+bootstrap+' '+environment+' --ready '+str(final_ready)+'\n').encode())
    cmd('systemctl','daemon-reload');cmd('systemctl','start',unit)
    new=pid(unit);ready(final_ready,new,expected_hash)
    cmd('systemctl','stop',temporary)
    assert not Path('/proc/'+str(standby)).exists()
    ready(final_ready,new,expected_hash,True)
    assert cmd('systemctl','is-active',unit)=='active'
    record({'phase':'worker_active','unit':unit,'pid':new,'runtime_sha256':expected_hash})

def main():
    mode,expected=sys.argv[1:];assert mode in ['preflight','apply']
    assert socket.gethostname()=='iZt4n729nx7wezpcx0v6z1Z'
    assert cmd('git','-C',str(HERE),'rev-parse','HEAD')==expected
    assert not cmd('git','-C',str(HERE),'status','--porcelain','--untracked-files=no')
    assert len(M)==4 and all(sha(HERE/m['payload'])==m['after_sha256'] and sha(ROOT/m['target'])==m['before_sha256'] for m in M)
    
    for m in M:compile((HERE/m['payload']).read_text(),m['target'],'exec')
    assert shutil.disk_usage(ROOT).free>1024**3
    assert int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))>1024*1024
    with (ROOT/'deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before=inventory();assert '127.0.0.1:8080' in before['nginx'] and 'api.luoxuecong.asia' in before['tunnel']
        routes=json.loads(dict(before['settings'])['route_reception_config'])['routing']['enabled_route_variants'];assert len(routes)==9 and 'yunnan_tw_8d' in routes
        if mode=='preflight':print(json.dumps({'status':'passed','commit':expected,'files':len(before['files']),'routes':routes}));return
        assert not BACK.exists();BACK.mkdir(parents=True,mode=0o700)
        (BACK/'inventory.json').write_text(json.dumps(before));os.chmod(BACK/'inventory.json',0o600)
        for m in M:
            p=BACK/m['target'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/m['target']).read_bytes())
        # No data migration: inventory already snapshots schema, settings and assets.
        # Never copy the multi-GB live SQLite database while holding its read lock;
        # rollback restores only release files and preserves all new customer writes.
        assert before['pids']==inventory()['pids'] and before['api_pid']==pid('china2go-api')
        try:
            for m in M:atomic(ROOT/m['target'],(HERE/m['payload']).read_bytes())
            runtime_hash=next(m['after_sha256'] for m in M if m['target'].endswith('runtime.py'))
            handoff('china2go-playground','playground',runtime_hash)
            handoff('china2go-worker','live',runtime_hash)
            after=inventory();targets={str(ROOT/m['target']):m['after_sha256'] for m in M}
            assert all(after['files'].get(p)==targets.get(p,h) for p,h in before['files'].items())
            assert all(before[k]==after[k] for k in ['settings','assets','schema','nginx','tunnel','api_pid'])
            result={'status':'switched','commit':expected,'runtime_sha256':runtime_hash,'api_pid_unchanged':after['api_pid'],'workers':after['pids'],'protected_files':len(before['files']),'public_verification':'pending'}
            (BACK/'result.json').write_text(json.dumps(result));record(result)
        except Exception as exc:
            record({'status':'failed_requires_recovery','error':str(exc)})
            # Restore source immediately; running prepared workers retain their loaded code.
            # Preserve active instances; recovery must hand off to restored code, never kill all.
            for m in M:
                if sha(ROOT/m['target'])==m['after_sha256']:atomic(ROOT/m['target'],(BACK/m['target']).read_bytes())
            raise

if __name__=='__main__':main()
