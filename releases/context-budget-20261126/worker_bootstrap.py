"""Prepared worker handoff: import first, then acquire exclusive scheduling ownership."""
import argparse, fcntl, hashlib, json, os, signal, sys, time
from pathlib import Path

def wait_pid(pid):
    while pid and Path('/proc/'+str(pid)).exists():
        time.sleep(.1)

def acquire_lock(path,on_active):
    stream=path.open('a+b')
    fcntl.flock(stream,fcntl.LOCK_EX)
    on_active()
    return stream

def main():
    p=argparse.ArgumentParser();p.add_argument('environment',choices=['live','playground']);p.add_argument('--ready',required=True);p.add_argument('--wait-pid',type=int,default=0);p.add_argument('--prepare-only',action='store_true');a=p.parse_args()
    root=Path('/opt/china2go-ai/backend');os.chdir(root);sys.path.insert(0,str(root))
    signal.signal(signal.SIGINT,signal.default_int_handler)
    from app.reception_v3 import runtime
    from app.db import SessionLocal
    from app.live_reply import assert_worker_ready
    with SessionLocal() as db: assert_worker_ready(db)
    if a.environment=='live':
        from app import live_reply_worker as worker
    else:
        from app import playground_worker as worker
    def state(value):
        dest=Path(a.ready);tmp=dest.with_suffix('.tmp');tmp.write_text(json.dumps({'pid':os.getpid(),'state':value,'runtime_sha256':hashlib.sha256(Path(runtime.__file__).read_bytes()).hexdigest()}));os.replace(tmp,dest)
    def acquire():
        return acquire_lock(root/('data/live-reply.lock' if a.environment=='live' else 'data/playground-reply.lock'),lambda:state('active'))
    state('prepared')
    if a.prepare_only:return
    wait_pid(a.wait_pid)
    if a.environment=='live':
        worker.acquire_lock=acquire
        worker.run()
    else:
        with acquire(): worker.run()

if __name__=='__main__':main()
