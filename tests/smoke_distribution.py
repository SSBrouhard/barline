"""Smoke an installed wheel or sdist from outside the source checkout.

Usage: python tests/smoke_distribution.py /absolute/path/to/venv/bin/python
The caller builds and installs the artifact into a fresh virtual environment.
This script never builds, installs, uploads, or changes native client settings.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def smoke(python):
    python = python.absolute()
    commands = {name: python.parent / name for name in ('barline', 'barline-recover', 'barline-hook')}
    for path in commands.values():
        require(path.is_file(), 'Missing installed console command: ' + str(path))
    # An inherited source PYTHONPATH must not hide a broken distribution.
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env['PYTHONNOUSERSITE'] = '1'
    with tempfile.TemporaryDirectory(prefix='barline-installed-') as temporary:
        root = Path(temporary).resolve()
        repo = root / 'repo'
        repo.mkdir()

        def run(argv, expected=0, payload=None):
            result = subprocess.run(list(map(str, argv)), cwd=root, env=env,
                                    input=payload, capture_output=True, text=True, timeout=30)
            require(result.returncode == expected,
                    f'{argv!r}: expected {expected}, got {result.returncode}\n{result.stdout}{result.stderr}')
            return result.stdout

        def json_run(argv, **kwargs):
            return json.loads(run(argv, **kwargs))

        installed = json_run([python, '-c',
            'import barline,json,sys; import importlib.metadata as m; '
            'print(json.dumps({"version":m.version("barline"),"file":barline.__file__, '
            '"prefix":sys.prefix,"requires":m.requires("barline")}))'])
        require(Path(installed['file']).is_relative_to(Path(installed['prefix'])),
                'Imported Barline from outside the isolated installation')
        require(not installed['requires'], 'Unexpected third-party runtime dependencies')
        run([python, '-m', 'barline', '--help'])
        run([commands['barline-recover'], '--help'])
        run([commands['barline-hook'], '--help'])
        for args in (('init', '-q', '-b', 'smoke/installed'),
                     ('config', 'user.name', 'Barline distribution smoke'),
                     ('config', 'user.email', 'smoke@example.invalid')):
            run(['git', '-C', repo, *args])
        source = repo / 'source.txt'
        source.write_text('original\n')
        run(['git', '-C', repo, 'add', '.'])
        run(['git', '-C', repo, 'commit', '-qm', 'fixture'])
        state = root / 'session.json'
        contract = root / 'contract.json'
        contract.write_text(json.dumps({
            'goal': 'Verify installed Barline behavior', 'owner_request': 'distribution smoke',
            'mode': 'local', 'checks': {'test': {'argv': [str(python), '-c', 'print("ok")'],
                                               'timeout_seconds': 5}},
            'required_external': [], 'must_happen': ['check runs'],
            'must_never': ['accept stale evidence'], 'non_goals': ['publish'],
            'policy_sources': ['distribution smoke fixture'],
        }))

        def cli(*args, **kwargs):
            return json_run([commands['barline'], '--state', state, *args], **kwargs)

        def accept():
            cli('run', '--task', 'one', '--check', 'test')
            cli('assess', '--task', 'one', '--outcome', 'met', '--evidence', 'Observed fixture behavior')
            result = cli('gate', '--task', 'one')
            require(result['validation'] == 'PASS', result)
            return result

        cli('init')
        cli('start', '--task', 'one', '--repo', repo, '--contract', contract)
        accept()
        binding = root / 'binding.json'

        def enroll(path, checkout, ledger, session):
            result = json_run([commands['barline-hook'], 'enroll', '--client', 'codex',
                              '--session-id', session, '--repo', checkout, '--state', ledger,
                              '--task', 'one', '--binding', path])
            require(result['decision'] == 'enrolled', result)

        def native(path, checkout, session):
            event = {'hook_event_name': 'Stop', 'session_id': session,
                     'turn_id': 'installed-turn', 'cwd': str(checkout), 'stop_hook_active': False}
            return json_run([commands['barline-hook'], 'native', '--client', 'codex',
                             '--binding', path], payload=json.dumps(event))

        enroll(binding, repo, state, 'installed-session')
        require(native(binding, repo, 'installed-session') == {}, 'Accepted native stop did not allow completion')
        source.write_text('edited\n')
        stale = cli('gate', '--task', 'one', expected=1)
        require(stale['validation'] != 'PASS' and 'test' in stale['missing'], stale)
        require(native(binding, repo, 'installed-session').get('decision') == 'block',
                'Native stop accepted stale check evidence')
        accepted = accept()
        observation = cli('observe', '--task', 'one')
        require(observation['validation'] == 'PASS', observation)
        archive = root / 'checkpoint.tar.gz'
        cli('checkpoint', '--task', 'one', '--output', archive, '--next', 'Resume smoke verification')
        recovered = root / 'recovered'
        with tarfile.open(archive) as package:
            guidance = package.extractfile('resume.md').read().decode()
        blocks = re.findall(r'```sh\n(.*?)```', guidance, re.S)
        require(len(blocks) == 1, 'Expected one executable recovery block')
        receipt = json_run(['sh', '-c', blocks[0], 'resume', recovered])
        require(receipt['fingerprint'] == accepted['fingerprint'], receipt)
        state = recovered / 'state.json'
        enroll(root / 'recovered-binding.json', recovered / 'repo', state, 'recovered-session')
        cli('durable', '--task', 'one', '--location', 'Disposable local smoke fixture storage receipt')
        cli('resume', '--task', 'one', '--expect', receipt['fingerprint'])
        require(cli('gate', '--task', 'one')['validation'] == 'PASS', 'Recovered gate failed')
        # Exercise the console recovery command separately from generated guidance.
        second = json_run([commands['barline-recover'], archive, root / 'second-recovery', '--task', 'one'])
        require(second['fingerprint'] == receipt['fingerprint'], second)
        print(json.dumps({'status': 'PASS', 'version': installed['version'],
                          'checks': ['installed module and console commands', 'check PASS',
                                     'edit invalidates evidence', 'native allow and block',
                                     'supervised observe', 'generated recovery and resume',
                                     'recovered hook enrollment', 'recovery console command']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('python', type=Path, help='Python inside the fresh installation virtual environment')
    smoke(parser.parse_args().python)
