"""Git-pinned seven-module release; reuse verified sequential worker handoff."""
import hashlib, importlib.util, json, os, shutil, socket, sys, fcntl
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path('/opt/china2go-ai')
BACK = ROOT / 'backups/traveler-permits-20261127'
spec = importlib.util.spec_from_file_location('handoff_release', HERE.parent / 'context-budget-20261126/publish.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
base.BACK = BACK
FILES = json.loads((HERE / 'manifest.json').read_text())['files']

def current(path):
    return base.sha(path) if path.exists() else None

def validate_sources():
    for m in FILES:
        assert current(ROOT / m['path']) == m['baseline_sha256'], 'live drift: ' + m['path']
        assert current(HERE / 'payload' / m['path']) == m['candidate_sha256']
        compile((HERE / 'payload' / m['path']).read_text(), m['path'], 'exec')

def recover():
    for m in FILES:
        target = ROOT / m['path']
        assert current(target) in (m['baseline_sha256'], m['candidate_sha256']), 'concurrent change'
        if m['baseline_sha256']:
            base.atomic(target, (BACK / m['path']).read_bytes())
        elif target.exists():
            target.unlink()
    old_hash = next(m['baseline_sha256'] for m in FILES if m['path'].endswith('/runtime.py'))
    for unit, env in [('china2go-playground', 'playground'), ('china2go-worker', 'live')]:
        base.handoff(unit, env, old_hash)
    base.record({'status': 'rolled_back', 'runtime_sha256': old_hash})

def main():
    mode, commit = sys.argv[1:]
    assert mode in ('preflight', 'apply', 'recover')
    assert socket.gethostname() == 'iZt4n729nx7wezpcx0v6z1Z'
    assert base.cmd('git', '-C', str(HERE), 'rev-parse', 'HEAD') == commit
    assert not base.cmd('git', '-C', str(HERE), 'status', '--porcelain', '--untracked-files=no')
    with (ROOT / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if mode == 'recover':
            recover(); return
        validate_sources()
        assert shutil.disk_usage(ROOT).free > 1024**3
        assert int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:'))) > 1024*1024
        base.cmd('nginx', '-t')
        before = base.inventory()
        assert '127.0.0.1:8080' in before['nginx']
        assert 'http://127.0.0.1:8000' in before['tunnel']
        routes = json.loads(dict(before['settings'])['route_reception_config'])['routing']['enabled_route_variants']
        assert len(routes) == 9 and 'yunnan_tw_8d' in routes
        if mode == 'preflight':
            print(json.dumps({'status': 'passed', 'commit': commit, 'files': len(before['files']), 'routes': routes})); return
        assert not BACK.exists()
        BACK.mkdir(mode=0o700)
        (BACK / 'inventory.json').write_text(json.dumps(before))
        os.chmod(BACK / 'inventory.json', 0o600)
        for m in FILES:
            if m['baseline_sha256']:
                p = BACK / m['path']; p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes((ROOT / m['path']).read_bytes())
        validate_sources()
        assert before['pids'] == base.inventory()['pids'] and before['api_pid'] == base.pid('china2go-api')
        try:
            for m in FILES:
                base.atomic(ROOT / m['path'], (HERE / 'payload' / m['path']).read_bytes())
            runtime_hash = next(m['candidate_sha256'] for m in FILES if m['path'].endswith('/runtime.py'))
            for unit, env in [('china2go-playground', 'playground'), ('china2go-worker', 'live')]:
                base.handoff(unit, env, runtime_hash)
            after = base.inventory()
            targets = {str(ROOT / m['path']): m['candidate_sha256'] for m in FILES}
            assert all(after['files'].get(p) == targets.get(p, h) for p, h in before['files'].items())
            assert all(after['files'].get(p) == h for p, h in targets.items())
            assert set(after['files']) - set(before['files']) == {p for p in targets if p not in before['files']}
            for k in ['settings', 'assets', 'schema', 'nginx', 'tunnel', 'api_pid']:
                assert before[k] == after[k], k + ' changed'
            result = {'status': 'switched', 'commit': commit, 'runtime_sha256': runtime_hash,
                      'workers': after['pids'], 'api_pid_unchanged': after['api_pid'],
                      'protected_files': len(before['files']), 'public_verification': 'pending'}
            (BACK / 'result.json').write_text(json.dumps(result)); base.record(result)
        except Exception as exc:
            base.record({'status': 'failed_requires_recovery', 'error': str(exc)})
            # Keep already running capacity; recovery uses the same prepared handoff.
            raise

if __name__ == '__main__':
    main()
