import importlib.util
from io import BytesIO
from pathlib import Path
import shlex
import time
import urllib.error
import urllib.request

import pytest


def load_deployer():
    path = Path(__file__).resolve().parents[2] / 'scripts/deploy_rujing2.py'
    spec = importlib.util.spec_from_file_location('rujing2_deployer_health', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('ready_after', [3, None])
def test_healthcheck_waits_for_listener_but_fails_within_deadline(monkeypatch, ready_after):
    deployer = load_deployer()
    clock = [0.0]
    attempts = []
    commands = []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def urlopen(url, timeout):
        attempts.append((url, timeout))
        assert 0 < timeout <= 5
        if ready_after is None or len(attempts) <= ready_after:
            raise urllib.error.URLError('connection refused during startup')
        return BytesIO(b'{"status":"ok"}')

    def run(client, command):
        commands.append(command)
        if command.startswith('python3 -c '):
            exec(shlex.split(command)[-1], {})

    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(deployer, 'run', run)
    if ready_after is None:
        with pytest.raises(urllib.error.URLError):
            deployer.check(object())
        assert clock[0] == 45
        assert len(commands) == 1
    else:
        deployer.check(object())
        assert len(attempts) == ready_after + 1
        assert clock[0] == 6
        assert commands[-1] == 'systemctl is-active rujing2-api rujing2-playground nginx'
