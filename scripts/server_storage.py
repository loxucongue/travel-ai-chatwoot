#!/usr/bin/env python3
"""Consistent SQLite backups and bounded retention for the production host.

Retention is a dry run unless --apply is supplied. Production systemd units
serialize both operations with the same lock. No customer/API calls are made.
"""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time

GIB = 1024 ** 3
DAILY = re.compile(r"app-\d{8}-\d{6}(?:-\d{6}Z)?\.db\Z")
RELEASE = re.compile(r"[A-Za-z0-9_-]*20\d{6}[A-Za-z0-9_-]*\Z")


def layout(root):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise ValueError('storage_root_must_be_canonical')
    for name in ('backups', 'releases'):
        path = root / name
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != root:
            raise ValueError('unsafe_storage_directory:' + name)
    return root


def inventory(path):
    """Refuse linked/special files; fingerprint metadata to detect later changes."""
    entries = []
    paths = [path, *path.rglob('*')] if path.is_dir() else [path]
    for item in paths:
        info = item.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('linked_or_special_file:' + str(item))
        entries.append((str(item.relative_to(path)), info.st_size, info.st_mtime_ns,
                        info.st_ino, stat.S_ISREG(info.st_mode)))
    return {
        'path': str(path), 'bytes': sum(e[1] for e in entries if e[4]),
        'modified': max(e[2] for e in entries) / 1e9,
        'fingerprint': hashlib.sha256(json.dumps(sorted(entries)).encode()).hexdigest(),
    }


def retention_plan(root, now=None, daily_days=14, release_days=30, keep=3):
    root = layout(root)
    if min(daily_days, release_days, keep) < 1:
        raise ValueError('retention_must_be_positive')
    now = time.time() if now is None else now
    manifest = json.loads((root / 'current-release.json').read_text())
    name = manifest['release']
    if not name or Path(name).name != name or name in ('.', '..'):
        raise ValueError('invalid_release_name')
    protected = {root / 'releases' / name, Path(manifest['backup'])}
    if not all(p.exists() and not p.is_symlink() and p.resolve() == p
               and p.parent in (root / 'backups', root / 'releases') for p in protected):
        raise ValueError('invalid_current_release_paths')
    candidates, skipped = [], []
    for category in ('backups', 'releases'):
        directories, daily = [], []
        for path in (root / category).iterdir():
            if path.is_symlink():
                skipped.append({'path': str(path), 'reason': 'symlink'})
                continue
            target = (directories if path.is_dir() and RELEASE.fullmatch(path.name)
                      else daily if category == 'backups' and path.is_file()
                      and DAILY.fullmatch(path.name) else None)
            if target is None:
                continue
            try:
                target.append(inventory(path))
            except ValueError as exc:
                skipped.append({'path': str(path), 'reason': str(exc)})
        for group, days in ((directories, release_days), (daily, daily_days)):
            group.sort(key=lambda x: (x['modified'], x['path']), reverse=True)
            protected.update(Path(x['path']) for x in group[:keep])
            candidates.extend(x for x in group[keep:]
                              if x['modified'] < now - days * 86400
                              and Path(x['path']) not in protected)
    return {'candidates': candidates, 'protected': sorted(map(str, protected)),
            'skipped': skipped, 'policy': {'daily_days': daily_days,
                                         'release_days': release_days, 'keep': keep}}


def referenced_candidates(root, candidates):
    """Check actual process use, filesystem links, and local systemd references."""
    found = set()
    targets = [Path(x['path']) for x in candidates]

    def record(value):
        for target in targets:
            if value == target or target in value.parents:
                found.add(str(target))

    proc = Path('/proc')
    if not proc.is_dir():
        raise RuntimeError('retention_requires_linux_process_inspection')
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            links = [process / 'cwd', process / 'exe', *list((process / 'fd').iterdir())]
            for link in links:
                try:
                    record(link.resolve(strict=True))
                except (FileNotFoundError, ProcessLookupError):
                    pass
            maps = (process / 'maps').read_text()
            for target in targets:
                if str(target) + '/' in maps or str(target) + '\n' in maps:
                    found.add(str(target))
        except (FileNotFoundError, ProcessLookupError):
            continue
        # Permission failures deliberately abort instead of assuming no references.
    for base in (root, Path('/etc/systemd/system'), Path('/etc/nginx')):
        for parent, dirs, files in os.walk(base, followlinks=False):
            for name in dirs + files:
                path = Path(parent) / name
                if path.is_symlink():
                    record(path.resolve())
    for unit in Path('/etc/systemd/system').glob('*.service'):
        if not unit.is_symlink():
            text = unit.read_text()
            for target in targets:
                if str(target) in text:
                    found.add(str(target))
    return found


def apply_retention(root, plan, reference_check=referenced_candidates):
    root = layout(root)
    # Never trust a previous plan after a deployment or another maintenance run.
    manifest = (root / 'current-release.json').read_bytes()
    fresh = retention_plan(root, **plan['policy'])
    eligible = {x['path']: x for x in fresh['candidates']}
    items = []
    for item in plan['candidates']:
        current = eligible.get(item['path'])
        if not current or current['fingerprint'] != item['fingerprint']:
            raise RuntimeError('retention_plan_changed:' + item['path'])
        items.append(current)
    referenced = reference_check(root, items)
    deleted, skipped = [], []
    for item in items:
        if (root / 'current-release.json').read_bytes() != manifest:
            raise RuntimeError('deployment_changed_during_retention')
        path = Path(item['path'])
        if str(path) in referenced:
            skipped.append({'path': str(path), 'reason': 'referenced'})
            continue
        if inventory(path)['fingerprint'] != item['fingerprint']:
            raise RuntimeError('retention_candidate_changed:' + str(path))
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        deleted.append(item)
    return {'deleted': deleted, 'skipped': skipped,
            'deleted_bytes': sum(x['bytes'] for x in deleted)}


def backup(root, reserve_bytes=2 * GIB):
    root = layout(root)
    source = root / 'backend/data/app.db'
    if source.is_symlink() or not source.is_file() or source.resolve() != source:
        raise ValueError('unsafe_or_missing_database')
    if reserve_bytes < 0:
        raise ValueError('negative_disk_reserve')
    # Read-only source prevents creating an empty production DB on a path typo.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30)) as src:
        size = src.execute('pragma page_count').fetchone()[0] * src.execute('pragma page_size').fetchone()[0]
        required = max(size, source.stat().st_size) + reserve_bytes
        free = shutil.disk_usage(root).free
        if free < required:
            raise RuntimeError(f'backup_disk_space_insufficient:required={required}:free={free}')
        name = 'app-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%fZ') + '.db'
        target = root / 'backups' / name
        fd, temporary = tempfile.mkstemp(prefix='.app-', suffix='.partial', dir=root / 'backups')
        os.close(fd)
        temporary = Path(temporary)
        try:
            with closing(sqlite3.connect(temporary)) as dst:
                def capacity_guard(status, remaining, total):
                    if shutil.disk_usage(root).free < reserve_bytes:
                        raise RuntimeError('backup_disk_reserve_exhausted')
                # A single snapshot avoids restarting small incremental copies
                # indefinitely while the live worker updates the source WAL.
                src.backup(dst, pages=-1, progress=capacity_guard)
                if dst.execute('pragma integrity_check').fetchall() != [('ok',)]:
                    raise RuntimeError('backup_integrity_failed')
            with temporary.open('r+b') as saved:
                os.fsync(saved.fileno())
            os.replace(temporary, target)
            if os.name == 'posix':
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return {'path': str(target), 'bytes': target.stat().st_size, 'integrity': 'ok'}
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def maintenance_lock(root, timeout=120):
    import fcntl  # Production runs on Linux; functions above also test on Windows.
    path = root / 'backups/.storage.lock'
    if path.is_symlink():
        raise ValueError('unsafe_lock_path')
    with path.open('a') as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('storage_maintenance_already_running')
                time.sleep(0.5)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('backup', 'retention'))
    parser.add_argument('--root', type=Path, default=Path('/opt/china2go-ai'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--daily-days', type=int, default=14)
    parser.add_argument('--release-days', type=int, default=30)
    parser.add_argument('--keep', type=int, default=3)
    parser.add_argument('--reserve-gib', type=float, default=2)
    args = parser.parse_args()
    root = layout(args.root)
    with maintenance_lock(root):
        if args.operation == 'backup':
            result = backup(root, int(args.reserve_gib * GIB))
        else:
            result = retention_plan(root, daily_days=args.daily_days,
                                    release_days=args.release_days, keep=args.keep)
            if args.apply:
                result = apply_retention(root, result)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
