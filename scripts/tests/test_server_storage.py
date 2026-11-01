"""Filesystem/SQLite regressions; no model, network or production access."""
import importlib.util
import gzip
import hashlib
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


def snapshot(root, name='release-20260101', filename='app-before.db', age_days=5):
    directory = root / 'backups' / name
    directory.mkdir(exist_ok=True)
    path = directory / filename
    with sqlite3.connect(path) as db:
        db.execute('create table records(content text)')
        db.execute('insert into records values (?)', ('retained business history ' * 10000,))
    db.close()
    timestamp = time.time() - age_days * 86400
    os.utime(path, (timestamp, timestamp))
    os.utime(directory, (timestamp, timestamp))
    return path


def compression(root):
    return storage.compression_plan(root, keep=1)


def compress(root, value=None, **kwargs):
    return storage.apply_compression(root, value or compression(root), lambda *_: set(), **kwargs)


def test_compressed_snapshot_restores_exact_bytes_sqlite_data_and_age(root):
    source = snapshot(root, age_days=60)
    original = source.read_bytes()
    file_time, directory_time = source.stat().st_mtime_ns, source.parent.stat().st_mtime_ns
    before = plan(root)['candidates'][0]['modified']
    result = compress(root)
    archive = Path(str(source) + '.gz')
    assert not source.exists() and gzip.decompress(archive.read_bytes()) == original
    assert result['saved_bytes'] > 100000
    assert result['archived'][0]['sha256_uncompressed'] == hashlib.sha256(original).hexdigest()
    assert archive.stat().st_mtime_ns == file_time
    assert source.parent.stat().st_mtime_ns == directory_time
    assert plan(root)['candidates'][0]['modified'] == before
    restored = root / 'restored.db'
    restored.write_bytes(gzip.decompress(archive.read_bytes()))
    with sqlite3.connect(restored) as db:
        assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
        assert db.execute('select content from records').fetchone()[0] == 'retained business history ' * 10000
    db.close()


def test_compression_preserves_recent_three_daily_current_release_and_live_database(root):
    source = database(root)
    for i in range(1, 6):
        p = root / 'backups' / f'app-2026010{i}-033000.db'
        p.write_bytes(source.read_bytes())
        timestamp = time.time() - (10 - i) * 86400
        os.utime(p, (timestamp, timestamp))
    current = snapshot(root, name='release-20261029')
    unknown = snapshot(root, name='customer-uploads')
    recent = snapshot(root, name='release-20260102', age_days=0)
    value = storage.compression_plan(root)
    assert {Path(x['path']).name for x in value['candidates']} == {'app-20260101-033000.db','app-20260102-033000.db'}
    assert source.exists() and current.exists() and unknown.exists() and recent.exists()


@pytest.mark.parametrize('extension', ['-wal', '-shm', '-journal'])
def test_compression_never_separates_sqlite_sidecar(root, extension):
    source = snapshot(root)
    sidecar = Path(str(source) + extension)
    sidecar.write_bytes(b'')
    timestamp = source.stat().st_mtime
    os.utime(sidecar, (timestamp, timestamp))
    os.utime(source.parent, (timestamp, timestamp))
    value = compression(root)
    assert not value['candidates']
    assert value['skipped'][0]['reason'] == 'sqlite_sidecar_present'


def test_bad_compression_verification_keeps_original(root, monkeypatch):
    source = snapshot(root)
    original = source.read_bytes()
    monkeypatch.setattr(storage, 'archive_digest', lambda _: ('bad hash', 0))
    with pytest.raises(RuntimeError, match='roundtrip_mismatch'):
        compress(root)
    assert source.read_bytes() == original
    assert not Path(str(source)+'.gz').exists()
    assert not list(source.parent.glob('*.partial'))


def test_compression_low_space_keeps_original(root, monkeypatch):
    source = snapshot(root)
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    with pytest.raises(RuntimeError, match='compression_disk_reserve'):
        compress(root)
    assert source.exists() and not list(source.parent.glob('*.partial'))


def test_archive_publish_failure_keeps_original(root, monkeypatch):
    source = snapshot(root)
    def fail(*_):
        raise OSError('archive publish failed')
    monkeypatch.setattr(storage.os, 'replace', fail)
    with pytest.raises(OSError, match='archive publish failed'):
        compress(root)
    assert source.exists() and not Path(str(source)+'.gz').exists()


def test_deployment_change_during_archive_keeps_original(root, monkeypatch):
    source = snapshot(root)
    original = storage.archive_digest
    def changed(path):
        result = original(path)
        with (root/'current-release.json').open('a') as stream:
            stream.write(' ')
        return result
    monkeypatch.setattr(storage, 'archive_digest', changed)
    with pytest.raises(RuntimeError, match='source_or_deployment_changed'):
        compress(root)
    assert source.exists() and not Path(str(source)+'.gz').exists()


def test_changed_or_injected_compression_plan_is_rejected(root, tmp_path_factory):
    source = snapshot(root)
    value = compression(root)
    outside = tmp_path_factory.mktemp('protected')/'valuable.db'
    outside.write_bytes(source.read_bytes())
    value['candidates'].append(storage.inventory(outside))
    with pytest.raises(RuntimeError, match='compression_plan_changed'):
        compress(root, value)
    assert outside.exists() and source.exists()


def test_referenced_snapshot_is_not_compressed(root):
    source = snapshot(root)
    result = storage.apply_compression(root, compression(root), lambda *_: {str(source)})
    assert source.exists() and not result['archived']
    assert result['skipped'][0]['reason'] == 'referenced'


def test_existing_archive_is_not_overwritten(root):
    source = snapshot(root)
    archive = Path(str(source)+'.gz')
    archive.write_bytes(b'previous archive')
    timestamp = source.stat().st_mtime
    os.utime(archive, (timestamp, timestamp))
    os.utime(source.parent, (timestamp, timestamp))
    value = compression(root)
    assert not value['candidates']
    assert value['skipped'][0]['reason'] == 'archive_already_exists'
    assert archive.read_bytes() == b'previous archive' and source.exists()


def test_retention_recognizes_old_compressed_daily_backups(root):
    source = database(root)
    for i, extension in enumerate(['.db.gz', '.db', '.db.gz', '.db', '.db'], start=1):
        path = root/'backups'/f'app-2026010{i}-033000{extension}'
        path.write_bytes(gzip.compress(source.read_bytes()) if extension.endswith('.gz') else source.read_bytes())
        timestamp = time.time() - (70-i) * 86400
        os.utime(path, (timestamp, timestamp))
    value = storage.retention_plan(root)
    assert {Path(x['path']).name for x in value['candidates']} == {'app-20260101-033000.db.gz','app-20260102-033000.db'}
    result = apply(root, value)
    assert len(result['deleted']) == 2
    assert len(list((root/'backups').glob('app-*'))) == 3


def test_failed_original_unlink_leaves_two_recoverable_copies(root, monkeypatch):
    source = snapshot(root)
    original = source.read_bytes()
    unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path == source:
            raise OSError('original unlink failed')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    with pytest.raises(OSError, match='original unlink failed'):
        compress(root)
    assert source.read_bytes() == original
    assert gzip.decompress(Path(str(source)+'.gz').read_bytes()) == original
