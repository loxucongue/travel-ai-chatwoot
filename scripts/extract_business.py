"""Preserve the original tracked business bundle before starting a new company.

Run once on the original tree. Never reads .env, customer databases or uploads.
"""
import hashlib
import argparse
import json
from pathlib import Path
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ref', default='90cbd6f', help='Original company source commit')
    args = parser.parse_args()
    commit = subprocess.check_output(['git', 'rev-parse', args.ref], cwd=ROOT, text=True).strip()
    target = ROOT / 'output/business-extract' / commit[:7]
    target.mkdir(parents=True, exist_ok=True)
    prefixes = ('data/knowledge/', 'docs/', 'backend/scripts/', 'backend/tests/',
                'backend/app/reception_v3/skills/')
    explicit = {'backend/app/reception_config.py', 'backend/app/reception_v3/prompt.py',
                'backend/app/reception_v3/route_choice.py', 'backend/app/reception_v3/runtime.py',
                'backend/app/route_packages.py', 'frontend/src/demo-api.ts',
                'scripts/deploy_v3.py', 'frontend/.env.production', 'cloudflare-relay/wrangler.jsonc'}
    manifest = {'source_commit': commit, 'files': {}}
    archive = target / 'china2go-business-complete.tar.gz'
    if archive.exists():
        raise SystemExit('Archive already exists; refusing to overwrite')
    # Git archives preserve non-ASCII paths; plain `git ls-files` may quote them.
    # Read the frozen commit instead of a modified working tree.
    subprocess.run(['git', 'archive', '--format=tar.gz', '-o', str(archive), commit,
                    *prefixes, *sorted(explicit)], cwd=ROOT, check=True)
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.isfile():
                manifest['files'][member.name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    manifest['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (target / 'manifest-complete.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.isfile():
                assert hashlib.sha256(tar.extractfile(member).read()).hexdigest() == manifest['files'][member.name]
    print(json.dumps({'archive': str(archive), 'files': len(manifest['files']), 'bytes': archive.stat().st_size}))


if __name__ == '__main__':
    main()
