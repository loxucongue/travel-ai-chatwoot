"""Restore this release only; keep data and API online, drain workers sequentially."""
import fcntl,json,socket
from pathlib import Path
import publish as p
assert socket.gethostname()=='iZt4n729nx7wezpcx0v6z1Z'
original=p.BACK;manifest=p.M
with (p.ROOT/'deploy.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert json.loads((original/'result.json').read_text())['status']=='switched'
    assert all(p.sha(p.ROOT/m['target'])==m['after_sha256'] for m in manifest),'later changes detected; do not overwrite'
    assert all(p.sha(original/m['target'])==m['before_sha256'] for m in manifest)
    p.BACK=original.with_name(original.name+'-rollback');p.BACK.mkdir(mode=0o700)
    before=p.inventory();(p.BACK/'inventory.json').write_text(json.dumps(before))
    for m in manifest:p.atomic(p.ROOT/m['target'],(original/m['target']).read_bytes())
    expected=next(m['before_sha256'] for m in manifest if m['target'].endswith('runtime.py'))
    p.handoff('china2go-playground','playground',expected)
    p.handoff('china2go-worker','live',expected)
    after=p.inventory();targets={str(p.ROOT/m['target']):m['before_sha256'] for m in manifest}
    assert all(after['files'].get(k)==targets.get(k,v) for k,v in before['files'].items())
    assert all(before[k]==after[k] for k in ['settings','assets','schema','nginx','tunnel','api_pid'])
    p.record({'status':'rolled_back','database_restored':False,'public_verification':'required'})
