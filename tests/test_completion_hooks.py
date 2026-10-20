"""Disposable-ledger tests for the shared completion boundary (no native clients)."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'lean.py'
HOOK = ROOT / 'hooks' / 'stop.py'


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.state = self.root / 'session.json'
        self.binding = self.root / 'context.json'
        for args in (('init', '-q'), ('config', 'user.email', 'test@example.invalid'),
                     ('config', 'user.name', 'Fixture')):
            self.git(*args)
        (self.repo / 'source').write_text('original\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        contract = {'goal': 'real fixture', 'owner_request': 'fixture owner', 'mode': 'local',
                    'checks': {'test': {'argv': [sys.executable, '-c', 'print("ok")'],
                                        'timeout_seconds': 3}},
                    'required_external': [], 'must_happen': ['tested'], 'must_never': ['fake pass'],
                    'non_goals': [], 'policy_sources': ['test']}
        self.contract = self.root / 'contract.json'
        self.contract.write_text(json.dumps(contract))
        self.lean('init')
        self.lean('start', '--task', 'one', '--repo', self.repo, '--contract', self.contract)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL)

    def lean(self, *args):
        p = subprocess.run([sys.executable, '-B', str(CLI), '--state', str(self.state), *map(str, args)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def request(self, operation='evaluate', **extra):
        request = {'operation': operation, 'binding': str(self.binding), 'client': 'codex',
                   'session_id': 'session-one', 'role': 'lead', 'repo': str(self.repo),
                   'turn_id': 'turn-one'}
        request.update(extra)
        return request

    def hook(self, operation='evaluate', timeout=4, env=None, **extra):
        p = subprocess.run([sys.executable, '-B', str(HOOK), 'shared', '--timeout', str(timeout)],
                           input=json.dumps(self.request(operation, **extra)),
                           capture_output=True, text=True, env=env, timeout=timeout + 3)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def enroll(self, **extra):
        options = {'state': str(self.state), 'task': 'one'}
        options.update(extra)
        result = self.hook('enroll', **options)
        self.assertEqual(result['decision'], 'enrolled', result)

    def accepted(self):
        self.lean('run', '--task', 'one', '--check', 'test')
        self.lean('assess', '--task', 'one', '--outcome', 'met', '--evidence', 'fixture behavior')

    def preserved(self):
        files = [p for p in self.repo.rglob('*') if p.is_file()]
        files += [self.state, Path(str(self.state) + '.lock')]
        logs = self.root / 'session-logs'
        files += list(logs.rglob('*')) if logs.exists() else []
        # Retain exact bytes while keeping hard-linked traversal fixtures small.
        physical, contents = {}, {}
        for path in files:
            if path.is_file():
                info = path.stat()
                key = (info.st_dev, info.st_ino)
                if key not in physical:
                    physical[key] = path.read_bytes()
                contents[str(path)] = physical[key]
        return contents

    def test_absent_generic_is_not_applicable_but_missing_enrollment_blocks(self):
        self.assertEqual(self.hook(binding=None)['decision'], 'not-applicable')
        before = self.preserved()
        self.assertEqual(self.hook()['decision'], 'block')
        self.assertEqual(self.preserved(), before)

    def test_observation_is_no_write_and_uses_repaired_gate(self):
        self.enroll()
        before = self.preserved()
        result = self.hook()
        self.assertEqual(result['decision'], 'block', result)
        self.assertEqual(result['validation'], 'INCONCLUSIVE')
        self.assertEqual(self.preserved(), before)
        self.accepted()
        before = self.preserved()
        self.assertEqual(self.hook()['decision'], 'complete')
        self.assertEqual(self.preserved(), before)
        (self.repo / 'source').write_text('edited\n')
        before = self.preserved()
        self.assertEqual(self.hook()['decision'], 'block')
        self.assertEqual(self.preserved(), before)

    def test_explicit_identity_and_two_sessions_on_same_checkout(self):
        self.enroll()
        self.accepted()
        other = self.root / 'other.json'
        original_state = self.state
        self.state = self.root / 'other-session.json'
        try:
            self.lean('init')
            self.lean('start', '--task', 'two', '--repo', self.repo, '--contract', self.contract)
            self.enroll(binding=str(other), session_id='other-session', task='two')
        finally:
            self.state = original_state
        self.assertEqual(self.hook()['decision'], 'complete')
        self.assertEqual(self.hook(binding=str(other), session_id='other-session')['decision'], 'block')
        for extra in ({'session_id': 'fork'}, {'client': 'grok'}, {'role': 'child'},
                      {'repo': str(self.root)}):
            with self.subTest(extra=extra):
                before = self.preserved()
                self.assertEqual(self.hook(**extra)['decision'], 'block')
                self.assertEqual(self.preserved(), before)

    def test_handoff_is_explicit_turn_bound_and_stale_on_tree_or_ledger_change(self):
        self.enroll()
        self.assertEqual(self.hook('handoff', reason='input', next='Ask owner')['decision'], 'block')
        self.assertEqual(self.hook('turn')['decision'], 'context-updated')
        self.assertEqual(self.hook('handoff', reason='input', next='Ask owner')['decision'], 'handoff-recorded')
        before = self.preserved()
        result = self.hook()
        self.assertEqual(result['decision'], 'handoff', result)
        self.assertEqual(self.hook(), result)
        self.assertEqual(self.preserved(), before)
        self.assertEqual(json.loads(self.state.read_text())['tasks']['one']['scope_outcome'], 'unknown')
        self.assertEqual(self.hook(turn_id='different')['decision'], 'block')
        self.hook('turn', turn_id='different')
        self.assertEqual(self.hook()['decision'], 'block')
        self.hook('handoff', reason='progress', next='Continue authorized check')
        (self.repo / 'source').write_text('changed\n')
        self.assertEqual(self.hook(turn_id='different')['decision'], 'block')
        self.hook('handoff', reason='external-blocker', next='Wait for access')
        self.lean('park', '--task', 'one', '--next', 'unrelated ledger record')
        self.assertEqual(self.hook(turn_id='different')['decision'], 'block')

    def test_busy_missing_lock_and_unsupported_ledger_preserve_bytes(self):
        self.enroll()
        with open(str(self.state) + '.lock', 'r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.preserved()
            self.assertEqual(self.hook()['decision'], 'block')
            self.assertEqual(self.preserved(), before)
        Path(str(self.state) + '.lock').unlink()
        before = self.preserved()
        self.assertEqual(self.hook()['decision'], 'block')
        self.assertEqual(self.preserved(), before)
        Path(str(self.state) + '.lock').touch()
        data = json.loads(self.state.read_text())
        data['version'] = 1
        self.state.write_text(json.dumps(data))
        before = self.preserved()
        self.assertEqual(self.hook()['decision'], 'block')
        self.assertEqual(self.preserved(), before)

    def test_paused_checkpoint_and_admission_stops_are_not_completion(self):
        self.enroll()
        self.accepted()
        self.lean('hypothesis', '--task', 'one', '--description', 'first', '--result', 'failed')
        self.lean('hypothesis', '--task', 'one', '--description', 'second', '--result', 'failed')
        result = self.hook()
        self.assertEqual(result['decision'], 'block')
        self.assertIn('diagnosis', result['reason'].lower())
        archive = self.root / 'pause.tgz'
        self.lean('checkpoint', '--task', 'one', '--output', archive, '--next', 'Inspect causal evidence')
        self.assertEqual(self.hook()['decision'], 'pause')
        archive.write_bytes(b'broken')
        self.assertEqual(self.hook()['decision'], 'block')
        archive.unlink()
        self.hook('turn')
        result = self.hook('handoff', reason='external-blocker', next='Recover missing checkpoint')
        self.assertEqual(result['decision'], 'handoff-recorded', result)
        self.assertEqual(self.hook()['decision'], 'handoff')

    def test_context_must_be_outside_every_target_checkout(self):
        second = self.root / 'second'
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(second)], check=True)
        self.lean('start', '--task', 'two', '--repo', second, '--contract', self.contract)
        result = self.hook('enroll', binding=str(second / 'context.json'), state=str(self.state), task='one')
        self.assertEqual(result['decision'], 'block')
        self.assertFalse((second / 'context.json').exists())
        self.assertFalse((second / 'context.json.lock').exists())

    def test_whole_process_deadline_kills_stalled_git_group(self):
        self.enroll()
        bindir = self.root / 'bin'
        bindir.mkdir()
        pidfile = self.root / 'pid'
        shim = bindir / 'git'
        shim.write_text('#!' + sys.executable + '\nimport os,time\n'
                        + 'open(' + repr(str(pidfile)) + ',"w").write(str(os.getpid()))\n'
                        + 'time.sleep(30)\n')
        shim.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        before = self.preserved()
        start = time.monotonic()
        result = self.hook(timeout=.4, env=env)
        self.assertEqual(result['decision'], 'block')
        self.assertLess(time.monotonic() - start, 2)
        self.assertIn('deadline', result['reason'].lower())
        self.assertEqual(self.preserved(), before)
        if pidfile.exists():
            pid = int(pidfile.read_text())
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                # Linux may retain a terminated orphan briefly as a zombie.
                proc = Path('/proc') / str(pid) / 'stat'
                if proc.exists() and proc.read_text().split()[2] == 'Z':
                    break
                time.sleep(.01)
            else:
                self.fail('Evaluator Git child still alive after deadline')

    def test_malformed_evidence_context_and_missing_repository_fail_closed(self):
        self.enroll()
        self.accepted()
        original = self.state.read_bytes()
        data = json.loads(original)
        data['tasks']['one']['evidence'][-1]['status'] = 'UNSUPPORTED'
        self.state.write_text(json.dumps(data))
        before = self.preserved()
        self.assertEqual(self.hook()['decision'], 'block')
        self.assertEqual(self.preserved(), before)
        self.state.write_bytes(original)
        binding = self.binding.read_bytes()
        for invalid in ('{', '{"version":99}', '[]'):
            self.binding.write_text(invalid)
            before = self.preserved()
            self.assertEqual(self.hook()['decision'], 'block')
            self.assertEqual(self.preserved(), before)
        self.binding.write_bytes(binding)
        moved = self.repo.with_name('moved')
        self.repo.rename(moved)
        try:
            self.assertEqual(self.hook()['decision'], 'block')
        finally:
            moved.rename(self.repo)

    def test_conflict_and_supersession_never_complete(self):
        self.enroll()
        self.accepted()
        self.lean('assess', '--task', 'one', '--outcome', 'conflict', '--evidence', 'changed scope')
        self.assertEqual(self.hook()['decision'], 'block')
        contract = json.loads(self.contract.read_text())
        contract['owner_request'] = 'explicitly authorized successor'
        self.contract.write_text(json.dumps(contract))
        self.lean('start', '--task', 'two', '--follows', 'one', '--repo', self.repo,
                  '--contract', self.contract)
        self.assertEqual(self.hook()['decision'], 'block')
        self.assertEqual(json.loads(self.binding.read_text())['task'], 'one')

    def test_context_updates_are_atomic_and_duplicate_turn_is_idempotent(self):
        self.enroll()
        before = self.preserved()
        workers = [subprocess.Popen([sys.executable, '-B', str(HOOK), 'shared'],
                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for _ in range(6)]
        payload = json.dumps(self.request('turn'))
        for worker in workers:
            worker.stdin.write(payload)
            worker.stdin.close()
            worker.stdin = None
        outcomes = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=8)
            self.assertEqual(worker.returncode, 0, stderr)
            outcomes.append(json.loads(stdout))
        self.assertTrue(any(r['decision'] == 'context-updated' for r in outcomes), outcomes)
        self.assertTrue(all(r['decision'] in ('context-updated', 'block') for r in outcomes))
        self.assertEqual(json.loads(self.binding.read_text())['turn_id'], 'turn-one')
        self.assertEqual(self.preserved(), before)
        self.hook('handoff', reason='input', next='Ask owner')
        original = self.binding.read_bytes()
        self.hook('turn')
        self.assertEqual(self.binding.read_bytes(), original)
        self.assertEqual(self.hook()['decision'], 'handoff')
        # Missing native prompt identity clears the old handoff rather than
        # letting the previous prompt supply a new prompt's permission to stop.
        self.hook('turn', turn_id=None)
        self.assertEqual(self.hook()['decision'], 'block')

    def test_changing_view_is_rejected_without_observer_writes(self):
        self.enroll()
        bindir = self.root / 'bin'
        bindir.mkdir()
        actual_git = subprocess.check_output(['which', 'git'], text=True).strip()
        ready = self.root / 'ready'
        proceed = self.root / 'proceed'
        shim = bindir / 'git'
        # Hold after the first complete snapshot. The mutation comes from an
        # independent writer, never the evaluator or Git itself.
        shim.write_text('#!' + sys.executable + '\nimport os,sys,time\n'
                        + 'if sys.argv[-4:] == ["diff", "--cached", "--binary", "HEAD"] and not os.path.exists('
                        + repr(str(ready)) + '):\n'
                        + ' open(' + repr(str(ready)) + ',"w").close()\n'
                        + ' while not os.path.exists(' + repr(str(proceed)) + '): time.sleep(.005)\n'
                        + 'os.execv(' + repr(actual_git) + ',[' + repr(actual_git) + ']+sys.argv[1:])\n')
        shim.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        worker = subprocess.Popen([sys.executable, '-B', str(HOOK), 'shared'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        worker.stdin.write(json.dumps(self.request()))
        worker.stdin.close()
        worker.stdin = None
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(.01)
        self.assertTrue(ready.exists(), 'Git fixture never reached snapshot boundary')
        (self.repo / 'source').write_text('independent writer\n')
        before = self.preserved()
        proceed.touch()
        stdout, stderr = worker.communicate(timeout=8)
        self.assertEqual(worker.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result['decision'], 'block', result)
        self.assertIn('changed during observation', result['reason'])
        self.assertEqual(self.preserved(), before)

    def test_deadline_covers_expensive_real_file_traversal(self):
        self.enroll()
        payload = self.repo / 'large'
        payload.write_bytes(b'x' * (2 * 1024 * 1024))
        for index in range(1000):
            os.link(payload, self.repo / ('large-' + str(index)))
        before = self.preserved()
        start = time.monotonic()
        result = self.hook(timeout=.3)
        self.assertEqual(result['decision'], 'block', result)
        self.assertIn('deadline', result['reason'].lower())
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.preserved(), before)

    def test_core_observe_cli_never_saves_or_creates_required_lock(self):
        self.accepted()
        before = self.preserved()
        result = self.lean('observe', '--task', 'one')
        self.assertEqual(result['validation'], 'PASS')
        self.assertEqual(self.preserved(), before)
        Path(str(self.state) + '.lock').unlink()
        before = self.preserved()
        p = subprocess.run([sys.executable, '-B', str(CLI), '--state', str(self.state),
                            'observe', '--task', 'one'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)['decision'], 'block')
        self.assertEqual(self.preserved(), before)


if __name__ == '__main__':
    unittest.main()
