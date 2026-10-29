"""Filesystem/SQLite regressions; no model, network or production access."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('server_storage', Path(__file__).parents[1] / 'server_storage.py')
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


@pytest.fixture
def root(tmp_path):
    for directory in ('backups', 'releases', 'backend/data'):
        (tmp_path / directory).mkdir(parents=True)
    for category in ('backups', 'releases'):
        (tmp_path / category / 'release-20261029').mkdir()
    (tmp_path / 'current-release.json').write_text(json.dumps({
        'release': 'release-20261029',
        'backup': str(tmp_path / 'backups/release-20261029'),
    }))
    return tmp_path.resolve()


def old_tree(root, name='release-20260101', category='backups', age_days=60):
    p = root / category / name
    p.mkdir()
    (p / 'app.db').write_bytes(b'old rollback')
    timestamp = time.time() - age_days * 86400
    os.utime(p / 'app.db', (timestamp, timestamp))
    os.utime(p, (timestamp, timestamp))
    return p


def plan(root, **kwargs):
    return storage.retention_plan(root, keep=1, **kwargs)


def apply(root, value):
    return storage.apply_retention(root, value, reference_check=lambda *_: set())


def database(root):
    path = root / 'backend/data/app.db'
    with sqlite3.connect(path) as db:
        db.execute('create table records(id integer primary key, content text)')
        db.execute("insert into records(content) values ('business history')")
    db.close()
    return path


def test_dry_run_preserves_files_and_only_selects_old_known_directories(root):
    old = old_tree(root)
    recent = old_tree(root, 'release-20261027', age_days=2)
    unknown = old_tree(root, 'customer-uploads')
    p = plan(root)
    assert [x['path'] for x in p['candidates']] == [str(old)]
    assert old.exists() and recent.exists() and unknown.exists()


def test_apply_deletes_only_validated_candidate(root):
    old = old_tree(root)
    result = apply(root, plan(root))
    assert not old.exists()
    assert result['deleted_bytes'] == len(b'old rollback')
    assert (root / 'backups/release-20261029').exists()


def test_current_release_is_kept_even_when_old(root):
    current = root / 'backups/release-20261029'
    at = time.time() - 90 * 86400
    os.utime(current, (at, at))
    old_tree(root, 'release-20261028', age_days=2)
    assert str(current) not in [x['path'] for x in plan(root)['candidates']]


def test_keep_latest_three_even_if_all_older_than_retention(root):
    for i in range(1, 6):
        old_tree(root, f'release-2026010{i}', age_days=60 + i)
    p = storage.retention_plan(root)
    assert len(p['candidates']) == 3  # Current + newest two old backups retained.


def test_daily_policy_keeps_recent_and_at_least_three(root):
    for day, age in [(1, 60), (2, 40), (3, 30), (4, 20), (5, 1)]:
        p = root / 'backups' / f'app-2026010{day}-033000.db'
        p.write_bytes(b'database')
        at = time.time() - age * 86400
        os.utime(p, (at, at))
    (root / 'backups/customer.db').write_bytes(b'keep')
    candidates = storage.retention_plan(root)['candidates']
    assert len(candidates) == 2
    assert all('20260101' in x['path'] or '20260102' in x['path'] for x in candidates)


def test_changed_candidate_aborts_without_deletion(root):
    old = old_tree(root)
    p = plan(root)
    (old / 'app.db').write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='plan_changed'):
        apply(root, p)
    assert old.exists()


def test_deployment_changed_current_release_is_protected(root):
    old = old_tree(root)
    p = plan(root)
    (root / 'current-release.json').write_text(json.dumps({
        'release': 'release-20261029', 'backup': str(old),
    }))
    with pytest.raises(RuntimeError, match='plan_changed'):
        apply(root, p)
    assert old.exists()


def test_referenced_candidate_is_skipped(root):
    old = old_tree(root)
    result = storage.apply_retention(root, plan(root), lambda *_: {str(old)})
    assert old.exists() and not result['deleted']
    assert result['skipped'][0]['reason'] == 'referenced'


def test_missing_manifest_aborts(root):
    (root / 'current-release.json').unlink()
    with pytest.raises(FileNotFoundError):
        plan(root)


def test_manifest_cannot_escape_root(root):
    (root / 'current-release.json').write_text(json.dumps({'release': '../outside', 'backup': str(root)}))
    with pytest.raises(ValueError, match='invalid_release_name'):
        plan(root)


def test_nonpositive_retention_rejected(root):
    with pytest.raises(ValueError, match='positive'):
        storage.retention_plan(root, keep=0)


def test_linked_candidates_never_deleted(root):
    old = old_tree(root)
    linked = old / 'linked'
    try:
        linked.symlink_to(root / 'backend/data', target_is_directory=True)
    except OSError:
        pytest.skip('Windows symlink privilege unavailable; also tested on Linux host')
    p = plan(root)
    assert not p['candidates'] and p['skipped']
    assert (root / 'backend/data').exists()


def test_successful_backup_is_readable_and_leaves_no_partial(root):
    source = database(root)
    result = storage.backup(root, reserve_bytes=0)
    path = Path(result['path'])
    with sqlite3.connect(path) as db:
        assert db.execute('select content from records').fetchone()[0] == 'business history'
        assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
    db.close()
    assert result['integrity'] == 'ok' and source.exists()
    assert not list((root / 'backups').glob('*.partial'))
    assert storage.DAILY.fullmatch(path.name)


def test_insufficient_space_keeps_old_backup_and_creates_nothing(root, monkeypatch):
    database(root)
    previous = root / 'backups/app-20261028-033000.db'
    previous.write_bytes(b'previous backup')
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    with pytest.raises(RuntimeError, match='disk_space_insufficient'):
        storage.backup(root)
    assert previous.read_bytes() == b'previous backup'
    assert len(list((root / 'backups').glob('app-*.db'))) == 1
    assert not list((root / 'backups').glob('*.partial'))


def test_space_exhausted_during_backup_cleans_partial(root, monkeypatch):
    database(root)
    calls = iter([100 * storage.GIB, 0])
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=next(calls, 0)))
    with pytest.raises(RuntimeError, match='reserve_exhausted'):
        storage.backup(root)
    assert not list((root / 'backups').glob('app-*.db'))
    assert not list((root / 'backups').glob('*.partial'))


def test_atomic_publish_failure_cleans_partial(root, monkeypatch):
    database(root)
    def fail(*_):
        raise OSError('simulated publish failure')
    monkeypatch.setattr(storage.os, 'replace', fail)
    with pytest.raises(OSError, match='publish failure'):
        storage.backup(root, reserve_bytes=0)
    assert not list((root / 'backups').glob('app-*.db'))
    assert not list((root / 'backups').glob('*.partial'))


def test_missing_source_is_not_created(root):
    with pytest.raises(ValueError, match='missing_database'):
        storage.backup(root)
    assert not (root / 'backend/data/app.db').exists()


def test_backup_includes_committed_wal_data(root):
    source = database(root)
    writer = sqlite3.connect(source)
    try:
        writer.execute('pragma journal_mode=wal')
        writer.execute("insert into records(content) values ('new WAL message')")
        writer.commit()
        result = storage.backup(root, reserve_bytes=0)
        reader = sqlite3.connect(result['path'])
        try:
            assert reader.execute('select count(*) from records').fetchone()[0] == 2
        finally:
            reader.close()
    finally:
        writer.close()


@pytest.mark.skipif(os.name != 'posix', reason='Linux production file locking')
def test_maintenance_lock_excludes_concurrent_backup_and_retention(root):
    with storage.maintenance_lock(root, timeout=0):
        with pytest.raises(RuntimeError, match='already_running'):
            with storage.maintenance_lock(root, timeout=0):
                pytest.fail('concurrent maintenance entered')
    with storage.maintenance_lock(root, timeout=0):
        pass


@pytest.mark.skipif(os.name != 'posix', reason='Linux process references')
def test_open_file_prevents_retention(root):
    old = old_tree(root)
    with (old / 'app.db').open('rb'):
        referenced = storage.referenced_candidates(root, plan(root)['candidates'])
        assert str(old) in referenced


def test_injected_candidate_cannot_escape_storage(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp('outside') / 'valuable.db'
    outside.write_bytes(b'keep')
    p = plan(root)
    p['candidates'].append(storage.inventory(outside))
    with pytest.raises(RuntimeError, match='plan_changed'):
        apply(root, p)
    assert outside.read_bytes() == b'keep'


def test_deployment_changes_during_reference_check_abort(root):
    old = old_tree(root)
    p = plan(root)
    def changed(*_):
        (root / 'current-release.json').write_text(json.dumps({
            'release': 'release-20261029', 'backup': str(old),
        }))
        return set()
    with pytest.raises(RuntimeError, match='deployment_changed_during'):
        storage.apply_retention(root, p, changed)
    assert old.exists()


def test_corrupt_source_never_publishes_a_backup(root):
    (root / 'backend/data/app.db').write_bytes(b'invalid sqlite data' * 100)
    with pytest.raises(sqlite3.DatabaseError):
        storage.backup(root, reserve_bytes=0)
    assert not list((root / 'backups').glob('app-*.db'))
    assert not list((root / 'backups').glob('*.partial'))
