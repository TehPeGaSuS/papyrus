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
    assert args.command is None and not args.start and not args.daemon and not args.dev      # None means "start"
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


def test_missing_venv_support_gives_a_helpful_message_and_creates_nothing(env_probe, monkeypatch, tmp_path):
    calls, venv_python = env_probe
    os.remove(venv_python)                      # no venv yet
    monkeypatch.setattr(papyrus, '_venv_support_available', lambda: False)
    with pytest.raises(SystemExit) as exc:
        papyrus.ensure_environment()
    assert 'python3-venv' in str(exc.value) and 'apt install' in str(exc.value)
    assert calls['pip'] == [] and calls['exec'] == []


def test_failed_venv_creation_leaves_no_half_made_venv(env_probe, monkeypatch):
    import subprocess
    import venv
    calls, venv_python = env_probe
    os.remove(venv_python)

    def broken_create(path, **kw):
        os.makedirs(os.path.join(path, 'bin'), exist_ok=True)
        open(os.path.join(path, 'bin', 'python'), 'w').close()   # what a partial venv looks like
        raise subprocess.CalledProcessError(1, 'ensurepip')

    monkeypatch.setattr(papyrus, '_venv_support_available', lambda: True)
    monkeypatch.setattr(venv, 'create', broken_create)
    with pytest.raises(SystemExit) as exc:
        papyrus.ensure_environment()
    assert 'python3-venv' in str(exc.value)
    assert not os.path.exists(papyrus._VENV_DIR)   # so the next run tries again properly
    assert calls['pip'] == []


def test_venv_support_check_reflects_the_interpreter():
    assert papyrus._venv_support_available() in (True, False)
    assert 'apt install python3-venv' in papyrus._venv_support_hint()


# ── stopping ──────────────────────────────────────────────────────────────────

def test_gunicorn_drains_quickly_on_stop():
    assert papyrus._server_options('127.0.0.1', 1027, 2)['graceful_timeout'] == 8
    assert papyrus._STOP_GRACE_SECONDS > 8                       # stop outwaits the graceful period


def stubbed_stop(monkeypatch, tmp_path, dies_on):
    """Run cmd_stop against a pretend process that exits when it receives one of `dies_on`."""
    sent, alive = [], {'now': True}
    pid_file = tmp_path / 'p.pid'
    pid_file.write_text('4242')
    monkeypatch.setattr(papyrus, '_PID_FILE', str(pid_file))
    monkeypatch.setattr(papyrus, '_alive', lambda pid: alive['now'])
    monkeypatch.setattr(papyrus.time, 'sleep', lambda s: None)
    def fake_kill(pid, sig):
        sent.append(sig)
        if sig in dies_on:
            alive['now'] = False
    monkeypatch.setattr(papyrus.os, 'kill', fake_kill)
    return sent, pid_file


def test_stop_is_graceful_first(monkeypatch, tmp_path, capsys):
    import signal
    sent, pid_file = stubbed_stop(monkeypatch, tmp_path, dies_on={signal.SIGTERM})
    papyrus.cmd_stop()
    assert sent == [signal.SIGTERM] and 'Stopped' in capsys.readouterr().out and not pid_file.exists()


def test_stop_forces_a_shutdown_that_lingers(monkeypatch, tmp_path, capsys):
    import signal
    sent, pid_file = stubbed_stop(monkeypatch, tmp_path, dies_on={signal.SIGQUIT})
    papyrus.cmd_stop()
    assert sent == [signal.SIGTERM, signal.SIGQUIT]
    out = capsys.readouterr()
    assert 'forcing the shutdown' in out.err and 'Stopped' in out.out and not pid_file.exists()


def test_stop_gives_up_with_a_message_if_nothing_works(monkeypatch, tmp_path):
    import signal
    sent, _ = stubbed_stop(monkeypatch, tmp_path, dies_on=set())
    with pytest.raises(SystemExit) as exc:
        papyrus.cmd_stop()
    assert 'kill -9 4242' in str(exc.value) and sent == [signal.SIGTERM, signal.SIGQUIT]


def test_a_copied_example_config_gets_a_helpful_error(tmp_path):
    """cp config.example.json config.json keeps the placeholder key: say which file, and how to fix it."""
    import shutil, subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ('app.py', 'config.example.json', 'pyproject.toml'):
        shutil.copy(os.path.join(root, name), tmp_path / name)
    shutil.copy(tmp_path / 'config.example.json', tmp_path / 'config.json')
    env = {k: v for k, v in os.environ.items() if k != 'SECRET_KEY'}
    env['PAPYRUS_BOOTSTRAPPED'] = '1'
    run = subprocess.run([sys.executable, '-I', str(tmp_path / 'app.py'), '--version'], capture_output=True, text=True,
                         env=env, cwd=tmp_path)
    assert run.returncode != 0 and 'Traceback' not in run.stderr
    assert 'config.json' in run.stderr and 'delete config.json' in run.stderr and 'secrets.token_hex(32)' in run.stderr


# ── the first run only bootstraps ─────────────────────────────────────────────

@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(papyrus, 'cmd_start', lambda args: calls.append('start'))
    monkeypatch.setattr(papyrus, 'cmd_stop', lambda: calls.append('stop'))
    monkeypatch.delenv('SECRET_KEY', raising=False)
    return calls


def test_the_first_run_only_sets_things_up(monkeypatch, started, capsys):
    monkeypatch.setattr(papyrus, '_FIRST_RUN', True)
    papyrus.main([])
    out = capsys.readouterr().out
    assert started == [] and 'not running yet' in out
    assert 'config.json' in out and 'python3 app.py admin' in out and 'python3 app.py --start' in out


def test_later_runs_start_the_server(monkeypatch, started):
    monkeypatch.setattr(papyrus, '_FIRST_RUN', False)
    papyrus.main([])
    papyrus.main(['-d'])
    assert started == ['start', 'start']


def test_other_commands_still_work_on_the_first_run(monkeypatch, started):
    monkeypatch.setattr(papyrus, '_FIRST_RUN', True)
    papyrus.main(['restart'])                        # an explicit restart is not "just bootstrap"
    assert started == ['stop', 'start']


def test_a_secret_key_in_the_environment_means_start_anyway(monkeypatch, started):
    monkeypatch.setattr(papyrus, '_FIRST_RUN', True)
    monkeypatch.setenv('SECRET_KEY', 'set-on-purpose')       # a container, say
    papyrus.main([])
    assert started == ['start']


def test_installing_the_environment_marks_the_run_as_first(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(papyrus, '_have_dependencies', lambda: False)
    monkeypatch.setattr(papyrus, '_python_has_dependencies', lambda python: False)
    monkeypatch.setattr(papyrus, '_VENV_DIR', str(tmp_path / '.venv'))
    monkeypatch.setattr(papyrus, '_venv_support_available', lambda: True)
    monkeypatch.setattr(papyrus.subprocess, 'check_call', lambda cmd: None)
    monkeypatch.setattr('venv.create', lambda *a, **k: (tmp_path / '.venv' / 'bin').mkdir(parents=True) or (tmp_path / '.venv' / 'bin' / 'python').write_text(''))
    monkeypatch.setattr(papyrus.os, 'execv', lambda python, argv: seen.update(first=os.environ.get('PAPYRUS_FIRST_RUN')))
    monkeypatch.delenv('PAPYRUS_BOOTSTRAPPED', raising=False)
    monkeypatch.delenv('PAPYRUS_FIRST_RUN', raising=False)
    try:
        papyrus.ensure_environment()
    finally:
        os.environ.pop('PAPYRUS_FIRST_RUN', None)
        os.environ.pop('PAPYRUS_BOOTSTRAPPED', None)
    assert seen == {'first': '1'}


@pytest.mark.parametrize('argv', [['--start'], ['start'], ['--start', '-d']])
def test_start_goes_ahead_even_on_the_first_run(monkeypatch, started, argv):
    monkeypatch.setattr(papyrus, '_FIRST_RUN', True)
    papyrus.main(argv)
    assert started == ['start']


def test_start_is_in_the_help():
    args = papyrus._parse_args(['--start'])
    assert args.start is True and args.command is None
    assert papyrus._parse_args([]).start is False


def test_the_readme_units_start_the_papyrus_command_with_start():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readme = open(os.path.join(root, 'README.md'), encoding='utf-8').read()
    lines = [l for l in readme.splitlines() if l.startswith('ExecStart=')]
    assert len(lines) == 2 and all(l.endswith('/.venv/bin/papyrus --start') for l in lines)
    assert 'WantedBy=default.target' in readme and 'enable-linger' in readme          # the user unit
    assert 'papyrus = ' in open(os.path.join(root, 'pyproject.toml'), encoding='utf-8').read()   # the command exists


# ── the access log shows the visitor, not the proxy ───────────────────────────

class _Resp:
    status, sent, headers = '200 OK', 12, []


def _logged_address(peer, forwarded_for=None):
    import datetime
    logger = papyrus._access_logger_class()
    environ = {'REMOTE_ADDR': peer, 'REQUEST_METHOD': 'GET', 'RAW_URI': '/', 'SERVER_PROTOCOL': 'HTTP/1.1'}
    if forwarded_for is not None:
        environ['HTTP_X_FORWARDED_FOR'] = forwarded_for
    atoms = logger.atoms(logger.__new__(logger), _Resp(), [], environ, datetime.timedelta(0))
    return atoms['h']


def test_the_access_log_shows_the_visitor_behind_a_trusted_proxy():
    assert _logged_address('127.0.0.1', '203.0.113.9') == '203.0.113.9'
    assert _logged_address('127.0.0.1', '198.51.100.1, 203.0.113.9') == '203.0.113.9'     # the proxy's own entry
    assert _logged_address('::1', '2001:db8::7') == '2001:db8::7'


def test_the_access_log_does_not_believe_headers_from_anyone_else():
    assert _logged_address('192.0.2.50', '203.0.113.9') == '192.0.2.50'     # not a trusted proxy: forged
    assert _logged_address('127.0.0.1') == '127.0.0.1'                       # a trusted proxy that sent nothing
    assert _logged_address('127.0.0.1', 'not-an-address') == '127.0.0.1'


def test_gunicorn_is_given_that_logger():
    assert papyrus._server_options('127.0.0.1', 5500, 2)['logger_class'].__name__ == 'ProxyAwareLogger'
