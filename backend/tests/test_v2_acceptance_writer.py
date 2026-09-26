import importlib.util
import json
import os
from pathlib import Path
import pytest


def load_writer(monkeypatch):
    script_dir = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(script_dir))
    path=os.environ.get('V2_EVALUATOR_TEST_SOURCE', str(script_dir / 'evaluate_v2_release.py'))
    spec=importlib.util.spec_from_file_location('acceptance_writer_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('failures',[1,7,8])
def test_windows_reader_contention_preserves_old_or_complete_new_result(monkeypatch,tmp_path,failures):
    module=load_writer(monkeypatch)
    target=tmp_path/'progress.json'
    target.write_text('{"completed":1}',encoding='utf-8')
    replace=Path.replace
    calls=[]
    def contend(path,destination):
        calls.append(destination)
        if len(calls)<=failures:
            assert json.loads(target.read_text())=={'completed':1}
            exc=PermissionError('target temporarily open')
            exc.winerror=32
            raise exc
        return replace(path,destination)
    monkeypatch.setattr(Path,'replace',contend)
    monkeypatch.setattr(module.time,'sleep',lambda seconds:None)
    if failures==8:
        with pytest.raises(PermissionError):module.write_atomic_json(target,{'completed':2})
        assert len(calls)==8 and json.loads(target.read_text())=={'completed':1}
        assert json.loads(target.with_suffix('.json.tmp').read_text())=={'completed':2}
    else:
        module.write_atomic_json(target,{'completed':2})
        assert len(calls)==failures+1 and json.loads(target.read_text())=={'completed':2}


def test_non_contention_permission_error_is_not_hidden(monkeypatch,tmp_path):
    module=load_writer(monkeypatch)
    def forbidden(*args):raise PermissionError('directory is read-only')
    monkeypatch.setattr(Path,'replace',forbidden)
    monkeypatch.setattr(module.time,'sleep',lambda seconds:pytest.fail('Not a Windows sharing conflict'))
    with pytest.raises(PermissionError):module.write_atomic_json(tmp_path/'progress.json',{'completed':2})
