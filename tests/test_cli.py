import json
import os
import stat
import sys

import pytest
from werkzeug.security import check_password_hash

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as papyrus  # noqa: E402

EXAMPLE = os.path.join(os.path.dirname(papyrus.__file__), 'config.example.json')


# ── config.example.json / first-run config ────────────────────────────────────

@pytest.fixture
def project(tmp_path):
    (tmp_path / 'config.example.json').write_text(open(EXAMPLE, encoding='utf-8').read())
    return tmp_path


def test_example_config_has_no_secrets():
    cfg = json.load(open(EXAMPLE, encoding='utf-8'))
    assert cfg['server']['secret_key'] == 'change-this-to-a-long-random-secret'
    assert cfg['admin']['pass'] == ''
    assert cfg['admin']['enabled'] is False
    assert cfg['server']['host'] == '127.0.0.1'


def test_ensure_config_creates_private_file_with_random_secret(project):
    assert papyrus.ensure_config(str(project)) is True
    path = project / 'config.json'
    cfg = json.loads(path.read_text())
    key = cfg['server']['secret_key']
    assert key != 'change-this-to-a-long-random-secret' and len(key) == 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert cfg['site'] == json.load(open(EXAMPLE, encoding='utf-8'))['site']


def test_ensure_config_never_overwrites(project):
    (project / 'config.json').write_text('{"mine": true}')
    assert papyrus.ensure_config(str(project)) is False
    assert json.loads((project / 'config.json').read_text()) == {'mine': True}


def test_each_install_gets_its_own_secret(tmp_path):
    keys = set()
    for name in ('a', 'b'):
        d = tmp_path / name
        d.mkdir()
        (d / 'config.example.json').write_text(open(EXAMPLE, encoding='utf-8').read())
        papyrus.ensure_config(str(d))
        keys.add(json.loads((d / 'config.json').read_text())['server']['secret_key'])
    assert len(keys) == 2


def test_ensure_config_requires_the_placeholder(tmp_path):
    (tmp_path / 'config.example.json').write_text('{"server": {"secret_key": "already-set"}}')
    with pytest.raises(RuntimeError):
        papyrus.ensure_config(str(tmp_path))


def test_ensure_environment_is_a_noop_when_dependencies_exist():
    assert papyrus._have_dependencies()
    assert papyrus.ensure_environment() is None  # would exec into .venv otherwise


# ── admin command ─────────────────────────────────────────────────────────────

def test_set_admin_password_enables_admin_and_keeps_the_rest(project):
    papyrus.ensure_config(str(project))
    path = str(project / 'config.json')
    before = json.load(open(path))
    papyrus.set_admin_password(path, 'root', 'correct horse battery')
    after = json.load(open(path))
    assert after['admin']['enabled'] is True and after['admin']['user'] == 'root'
    assert check_password_hash(after['admin']['pass'], 'correct horse battery')
    assert after['admin']['pass'] != 'correct horse battery'
    assert after['server'] == before['server'] and after['site'] == before['site']
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not os.path.exists(path + '.tmp')


def test_cmd_admin_validates_the_password(monkeypatch, project):
    monkeypatch.setattr(papyrus, '_BASE_DIR', str(project))
    papyrus.ensure_config(str(project))
    answers = iter(['short'])
    monkeypatch.setattr(papyrus.getpass, 'getpass', lambda prompt='': next(answers))
    with pytest.raises(SystemExit):
        papyrus.cmd_admin(papyrus._parse_args(['admin']))
    answers = iter(['long enough 1', 'long enough 2'])
    with pytest.raises(SystemExit):
        papyrus.cmd_admin(papyrus._parse_args(['admin']))
    answers = iter(['long enough 1', 'long enough 1'])
    papyrus.cmd_admin(papyrus._parse_args(['admin', '--user', 'ann']))
    cfg = json.load(open(project / 'config.json'))
    assert cfg['admin']['user'] == 'ann' and cfg['admin']['enabled']


# ── command line / server options ─────────────────────────────────────────────

def test_default_command_is_start():
    args = papyrus._parse_args([])
    assert args.command == 'start' and not args.daemon and not args.dev
    assert papyrus._parse_args(['-d']).daemon
    assert papyrus._parse_args(['--port', '1027', '--host', '127.0.0.1']).port == 1027
    with pytest.raises(SystemExit):
        papyrus._parse_args(['frobnicate'])


def test_gunicorn_options():
    opts = papyrus._server_options('127.0.0.1', 1027, 4, pidfile='/tmp/p.pid')
    assert opts['bind'] == '127.0.0.1:1027' and opts['workers'] == 4
    assert opts['pidfile'] == '/tmp/p.pid'
    assert opts['post_fork'] is papyrus._post_fork
    assert 'pidfile' not in papyrus._server_options('127.0.0.1', 1027, 4)


def test_gunicorn_accepts_our_options():
    """The options must be valid Gunicorn settings (catches typos without starting a server)."""
    from gunicorn.app.base import BaseApplication

    class Probe(BaseApplication):
        def load_config(self):
            for k, v in papyrus._server_options('127.0.0.1', 1027, 3, '/tmp/p.pid').items():
                if k in self.cfg.settings:
                    self.cfg.set(k, v)

        def load(self):
            return papyrus.app

    probe = Probe()
    assert probe.cfg.bind == ['127.0.0.1:1027'] and probe.cfg.workers == 3
    assert probe.cfg.worker_class_str == 'gthread' and probe.cfg.pidfile == '/tmp/p.pid'
    if 'control_socket' in probe.cfg.settings:
        assert os.path.basename(probe.cfg.control_socket) == 'papyrus-1027.ctl'


def test_post_fork_restarts_the_cleanup_thread(monkeypatch):
    started = []
    monkeypatch.setattr(papyrus, '_cleanup_running', True)
    monkeypatch.setattr(papyrus, '_start_cleanup_task', lambda: started.append(1))
    papyrus._post_fork(None, None)
    assert started == [1] and papyrus._cleanup_running is False


def test_default_workers_are_sane(monkeypatch):
    monkeypatch.setitem(papyrus._server, 'workers', 0)
    assert 2 <= papyrus._default_workers() <= 8
    monkeypatch.setitem(papyrus._server, 'workers', 3)
    assert papyrus._default_workers() == 3


# ── background management ─────────────────────────────────────────────────────

def test_status_and_stop_without_a_pid_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(papyrus, '_PID_FILE', str(tmp_path / 'none.pid'))
    papyrus.cmd_status()
    papyrus.cmd_stop()
    assert capsys.readouterr().out.splitlines() == ['Not running', 'Not running']


def test_stale_pid_file_is_cleaned_up(tmp_path, monkeypatch, capsys):
    pid_file = tmp_path / 'stale.pid'
    pid_file.write_text('999999999')  # no such process
    monkeypatch.setattr(papyrus, '_PID_FILE', str(pid_file))
    papyrus.cmd_stop()
    assert 'Not running' in capsys.readouterr().out and not pid_file.exists()


def test_status_reports_a_live_process(tmp_path, monkeypatch, capsys):
    pid_file = tmp_path / 'live.pid'
    pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(papyrus, '_PID_FILE', str(pid_file))
    papyrus.cmd_status()
    assert f'Running (PID {os.getpid()})' in capsys.readouterr().out


def test_port_in_use_detects_a_listener():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        s.listen()
        port = s.getsockname()[1]
        assert papyrus._port_in_use('127.0.0.1', port)
    assert not papyrus._port_in_use('127.0.0.1', port)


# ── packaging ─────────────────────────────────────────────────────────────────

def test_pyproject_declares_dependencies_and_the_papyrus_command():
    import tomllib
    meta = tomllib.load(open(os.path.join(os.path.dirname(papyrus.__file__), 'pyproject.toml'), 'rb'))
    names = {d.split('>')[0].split('<')[0].split('=')[0].strip().lower() for d in meta['project']['dependencies']}
    assert names == {'flask', 'gunicorn', 'mistune', 'nh3'}
    module, func = meta['project']['scripts']['papyrus'].split(':')
    assert module == 'app' and callable(getattr(papyrus, func))
    assert meta['tool']['setuptools']['py-modules'] == ['app']


def test_control_socket_is_named_after_the_project_and_port(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    assert papyrus._control_socket_path(1027) == str(tmp_path / 'papyrus-1027.ctl')
    assert papyrus._control_socket_path(5500) != papyrus._control_socket_path(1027)
    monkeypatch.delenv('XDG_RUNTIME_DIR')
    assert papyrus._control_socket_path(1027).endswith('papyrus-1027.ctl')


# ── first-run environment: install only when needed ───────────────────────────

@pytest.fixture
def env_probe(monkeypatch, tmp_path):
    """Pretend the dependencies are missing and record what ensure_environment does."""
    venv = tmp_path / '.venv'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'bin' / 'python').write_text('')
    calls = {'pip': [], 'exec': [], 'probe_ok': True}

    class Result:
        def __init__(self, code): self.returncode = code

    monkeypatch.setattr(papyrus, '_VENV_DIR', str(venv))
    monkeypatch.setattr(papyrus, '_have_dependencies', lambda: False)
    monkeypatch.setattr(papyrus.subprocess, 'run', lambda *a, **k: Result(0 if calls['probe_ok'] else 1))
    monkeypatch.setattr(papyrus.subprocess, 'check_call', lambda cmd, **k: calls['pip'].append(cmd))
    monkeypatch.setattr(papyrus.os, 'execv', lambda path, argv: calls['exec'].append(path))
    monkeypatch.setenv('PAPYRUS_BOOTSTRAPPED', '')
    return calls, str(venv / 'bin' / 'python')


def test_existing_venv_with_dependencies_is_not_reinstalled(env_probe):
    calls, venv_python = env_probe
    papyrus.ensure_environment()
    assert calls['pip'] == []              # no pip run, so no network needed
    assert calls['exec'] == [venv_python]


def test_venv_missing_dependencies_gets_papyrus_installed(env_probe):
    calls, venv_python = env_probe
    calls['probe_ok'] = False
    papyrus.ensure_environment()
    assert len(calls['pip']) == 1
    assert calls['pip'][0][:5] == [venv_python, '-m', 'pip', 'install', '--quiet']
    assert calls['pip'][0][5:7] == ['-e', papyrus._BASE_DIR]
    assert calls['exec'] == [venv_python]


def test_environment_setup_does_not_loop(env_probe, monkeypatch):
    monkeypatch.setenv('PAPYRUS_BOOTSTRAPPED', '1')
    with pytest.raises(SystemExit):
        papyrus.ensure_environment()
