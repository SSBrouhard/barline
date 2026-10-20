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


class CompletionFixture(unittest.TestCase):
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



class CompletionTests(CompletionFixture):
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

    def test_turn_retains_task_schema_repository_and_checkout_boundaries(self):
        self.enroll()
        self.accepted()
        original = self.state.read_bytes()
        context = self.binding.read_bytes()
        mutations = (
            lambda state: state.update(version=1),
            lambda state: state['tasks'].pop('one'),
            lambda state: state['tasks']['one'].update(work_state='UNKNOWN'),
            lambda state: state['tasks']['one'].update(repo=str(self.root)),
            lambda state: state['tasks']['one']['evidence'][-1].update(status='UNKNOWN'),
        )
        for mutate in mutations:
            data = json.loads(original)
            mutate(data)
            self.state.write_text(json.dumps(data))
            before = self.preserved()
            result = self.hook('turn')
            self.assertEqual(result['decision'], 'block', result)
            self.assertEqual(self.binding.read_bytes(), context)
            self.assertEqual(self.preserved(), before)
        self.state.write_bytes(original)
        lock = Path(str(self.state) + '.lock')
        lock.unlink()
        self.assertEqual(self.hook('turn')['decision'], 'block')
        self.assertFalse(lock.exists(), 'Turn validation created a ledger lock')
        lock.touch()
        second = self.root / 'second'
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(second)], check=True)
        self.lean('start', '--task', 'two', '--repo', second, '--contract', self.contract)
        moved = second / 'context.json'
        moved.write_bytes(context)
        moved_lock = Path(str(moved) + '.lock')
        moved_lock.touch()
        result = self.hook('turn', binding=str(moved))
        self.assertEqual(result['decision'], 'block', result)
        self.assertIn('outside every target checkout', result['reason'])
        self.assertEqual(moved.read_bytes(), context)
        self.assertEqual(moved_lock.read_bytes(), b'')

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


class NativeProtocolTests(CompletionFixture):
    # Process fixtures exercise protocol translation, not actual native loading.
    def event(self, client, event='Stop', **extra):
        payload = json.loads((ROOT / 'tests/fixtures/hooks' / (client + '-stop.json')).read_text())
        payload['cwd'] = str(self.repo)
        payload['hook_event_name'] = event
        if client == 'grok':
            payload['hookEventName'] = ''.join(('_' + char.lower()) if char.isupper() else char for char in event).lstrip('_')
        payload.update(extra)
        return payload

    def native(self, client, payload, binding=True, timeout=4, env=None):
        args = [sys.executable, '-B', str(HOOK), 'native', '--client', client, '--timeout', str(timeout)]
        if binding:
            args += ['--binding', str(self.binding)]
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        process = subprocess.run(args, input=raw, capture_output=True, text=True, timeout=timeout + 3, env=env)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(process.stderr, '')
        result = json.loads(process.stdout)
        self.assertIsInstance(result, dict)
        self.assertNotIn('continue', result)
        return result

    def enroll_native(self, client):
        self.enroll(client=client)
        return self.native(client, self.event(client, 'UserPromptSubmit'))

    def assert_prompt_allowed(self, client, result, diagnostic=None):
        self.assertNotEqual(result.get('decision'), 'block', result)
        if client == 'grok':
            self.assertEqual(result, {})
        else:
            context = result['hookSpecificOutput']['additionalContext']
            self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
            if diagnostic:
                self.assertIn(diagnostic, context)
                self.assertIn('Completion remains guarded', context)

    def test_prompt_errors_allow_input_but_stop_stays_closed(self):
        for client in ('claude', 'codex', 'grok'):
            self.binding = self.root / (client + '.json')
            self.enroll_native(client)
            original = self.binding.read_bytes()
            for broken in ('missing', 'corrupt', 'identity'):
                with self.subTest(client=client, broken=broken):
                    if broken == 'missing':
                        self.binding.unlink()
                    elif broken == 'corrupt':
                        self.binding.write_text('{')
                    else:
                        data = json.loads(original)
                        data['session_id'] = 'other-session'
                        self.binding.write_text(json.dumps(data))
                    try:
                        result = self.native(client, self.event(client, 'UserPromptSubmit'))
                        stop = self.native(client, self.event(client))
                    finally:
                        self.binding.write_bytes(original)
                    self.assert_prompt_allowed(client, result, 'unavailable')
                    self.assertEqual(stop['decision'], 'block')
            for locked_path in (str(self.state) + '.lock', str(self.binding) + '.lock'):
                with self.subTest(client=client, busy=locked_path):
                    with open(locked_path, 'rb') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        result = self.native(client, self.event(client, 'UserPromptSubmit'))
                        self.assert_prompt_allowed(client, result, 'busy')
                        self.assertEqual(self.native(client, self.event(client))['decision'], 'block')

    def test_failed_new_prompt_cannot_reuse_previous_handoff(self):
        for client in ('claude', 'codex', 'grok'):
            self.binding = self.root / (client + '.json')
            self.enroll_native(client)
            self.assertEqual(self.hook('handoff', client=client, reason='input', next='Ask owner')['decision'], 'handoff-recorded')
            turn_key = {'claude': 'prompt_id', 'codex': 'turn_id', 'grok': 'promptId'}[client]
            prompt = self.event(client, 'UserPromptSubmit', **{turn_key: 'new-human-turn'})
            with open(str(self.state) + '.lock', 'rb') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assert_prompt_allowed(client, self.native(client, prompt), 'busy')
            self.assertEqual(self.native(client, self.event(client, **{turn_key: 'new-human-turn'}))['decision'], 'block')
            self.assertEqual(self.native(client, self.event(client, **{turn_key: None}))['decision'], 'block')
            # A successful delivery records the new identity and drops the old
            # handoff; duplicate deliveries of the same identity stay harmless.
            self.assert_prompt_allowed(client, self.native(client, prompt))
            self.assertIsNone(json.loads(self.binding.read_bytes())['handoff'])

    def test_prompt_worker_deadline_allows_input_without_stale_handoff(self):
        for client in ('claude', 'codex', 'grok'):
            self.binding = self.root / (client + '.json')
            self.enroll_native(client)
            self.hook('handoff', client=client, reason='input', next='Ask owner')
            original = self.binding.read_bytes()
            self.binding.unlink()
            os.mkfifo(self.binding)
            turn_key = {'claude': 'prompt_id', 'codex': 'turn_id', 'grok': 'promptId'}[client]
            try:
                start = time.monotonic()
                result = self.native(client, self.event(client, 'UserPromptSubmit', **{turn_key: 'new-human-turn'}), timeout=.4)
                self.assertLess(time.monotonic() - start, 2)
                self.assert_prompt_allowed(client, result, 'deadline')
                stop = self.native(client, self.event(client, **{turn_key: 'new-human-turn'}), timeout=.4)
                self.assertEqual(stop['decision'], 'block', stop)
                self.assertIn('deadline', stop['reason'])
            finally:
                self.binding.unlink()
                self.binding.write_bytes(original)
            self.assertEqual(self.native(client, self.event(client, **{turn_key: 'new-human-turn'}))['decision'], 'block')

    def test_prompt_turn_does_not_invoke_git_or_hash_checkout(self):
        self.enroll_native('codex')
        payload = self.repo / 'large'
        payload.write_bytes(b'x' * (2 * 1024 * 1024))
        for index in range(128):
            os.link(payload, self.repo / ('large-' + str(index)))
        bindir = self.root / 'prompt-bin'
        bindir.mkdir()
        invoked = self.root / 'git-invoked'
        shim = bindir / 'git'
        shim.write_text('#!' + sys.executable + '\nimport pathlib,time\n' +
                        'pathlib.Path(' + repr(str(invoked)) + ').touch()\ntime.sleep(30)\n')
        shim.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        # Test Git admission, not interpreter startup speed on a shared runner.
        before = self.preserved()
        result = self.native('codex', self.event('codex', 'UserPromptSubmit', turn_id='new-turn'), timeout=5, env=env)
        self.assert_prompt_allowed('codex', result)
        self.assertFalse(invoked.exists(), 'Prompt identity recording invoked Git')
        self.assertEqual(json.loads(self.binding.read_bytes())['turn_id'], 'new-turn')
        self.assertEqual(self.preserved(), before)
        stop = self.native('codex', self.event('codex', turn_id='new-turn'), timeout=5, env=env)
        self.assertEqual(stop['decision'], 'block')
        self.assertIn('deadline', stop['reason'])
        self.assertTrue(invoked.exists(), 'Stop skipped full observation')

    def test_native_protocol_complete_block_handoff_and_nonapplicable(self):
        for client in ('claude', 'codex', 'grok'):
            with self.subTest(client=client):
                self.binding = self.root / (client + '.json')
                result = self.enroll_native(client)
                self.assertNotEqual(result.get('decision'), 'block', result)
                if client != 'grok':
                    self.assertIn('handoff', result['hookSpecificOutput']['additionalContext'])
                else:
                    self.assertEqual(result, {})
                payload = self.event(client)
                before = self.preserved()
                result = self.native(client, payload)
                self.assertEqual(result['decision'], 'block')
                self.assertIn('handoff', result['reason'])
                self.assertEqual(result, self.native(client, payload))
                self.assertEqual(before, self.preserved())
                self.assertEqual(self.native(client, payload, binding=False), {})
                self.assertEqual(self.hook('handoff', client=client, reason='input', next='Ask owner')['decision'], 'handoff-recorded')
                self.assertEqual(self.native(client, payload), {})
                self.assertEqual(json.loads(self.state.read_text())['tasks']['one']['scope_outcome'], 'unknown')
        self.accepted()
        for client in ('claude', 'codex', 'grok'):
            self.binding = self.root / (client + '.json')
            self.assertEqual(self.native(client, self.event(client)), {})

    def test_native_errors_are_valid_block_json(self):
        for client in ('claude', 'codex', 'grok'):
            for payload in ('{', '[]', '{}', self.event(client), self.event(client, cwd=None)):
                with self.subTest(client=client, payload=payload):
                    self.assertEqual(self.native(client, payload)['decision'], 'block')

    def test_irrelevant_child_and_teardown_before_missing_binding(self):
        for client in ('claude', 'codex', 'grok'):
            for event in ('SubagentStop', 'SessionEnd', 'Interrupt', 'StopFailure', 'StopCancelled'):
                self.assertEqual(self.native(client, self.event(client, event)), {})
            child = {'subagentType': 'worker'} if client == 'grok' else {'agent_id': 'child'}
            self.assertEqual(self.native(client, self.event(client, **child)), {})
        for reason in ('channel_closed', 'shutdown'):
            self.assertEqual(self.native('grok', self.event('grok', reason=reason)), {})

    def test_wrong_adapter_unknown_metadata_and_missing_identity_block(self):
        self.enroll_native('claude')
        for payload in (self.event('grok'), self.event('claude', agent_id=[]),
                        self.event('claude', session_id=''), self.event('claude', stop_hook_active='yes')):
            self.assertEqual(self.native('claude', payload)['decision'], 'block')
        self.binding = self.root / 'codex.json'
        self.enroll_native('codex')
        self.assertEqual(self.native('codex', self.event('codex', session_id='fork'))['decision'], 'block')
        self.assertEqual(self.native('codex', self.event('codex', turn_id=None))['decision'], 'block')

    def test_grok_unknown_background_never_means_empty(self):
        self.enroll_native('grok')
        self.accepted()
        for field in ('backgroundTasks', 'sessionCrons'):
            for value in (None, {}, [{'id': 'task', 'type': 'shell', 'status': 'running'}]):
                self.assertEqual(self.native('grok', self.event('grok', **{field: value}))['decision'], 'block')
            payload=self.event('grok'); del payload[field]
            self.assertEqual(self.native('grok', payload)['decision'], 'block')

    def test_codex_synthetic_turns_preserve_rejection_not_handoff(self):
        self.enroll_native('codex')
        first=self.native('codex', self.event('codex'))
        rejection=json.loads(self.binding.read_text())['rejection']
        self.native('codex', self.event('codex', 'UserPromptSubmit', turn_id='synthetic'))
        second=self.native('codex', self.event('codex', turn_id='synthetic', stop_hook_active=True))
        self.assertIn('Unchanged rejection', second['reason'])
        self.assertNotIn('Unchanged rejection', first['reason'])
        self.assertEqual(second, self.native('codex', self.event('codex', turn_id='synthetic', stop_hook_active=True)))
        self.assertEqual(json.loads(self.binding.read_text())['rejection']['signature'], rejection['signature'])
        self.assertEqual(self.hook('handoff', reason='progress', next='Continue later')['decision'], 'handoff-recorded')
        self.assertEqual(self.native('codex', self.event('codex', turn_id='synthetic', stop_hook_active=True)), {})
        self.native('codex', self.event('codex', 'UserPromptSubmit', turn_id='real-next'))
        self.assertEqual(self.native('codex', self.event('codex', turn_id='real-next'))['decision'], 'block')

    def test_native_cli_handoff_uses_binding_identity(self):
        self.enroll_native('codex')
        process=subprocess.run([sys.executable, '-B', str(HOOK), 'handoff', '--binding', str(self.binding),
                                '--reason', 'input', '--next', 'Ask owner'], capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        self.assertEqual(json.loads(process.stdout)['decision'],'handoff-recorded')
        self.assertEqual(self.native('codex',self.event('codex')), {})


    def test_native_pause_is_allow_without_ledger_mutation(self):
        self.accepted()
        archive=self.root / 'pause.tgz'
        self.lean('checkpoint', '--task', 'one', '--output', archive, '--next', 'Resume later')
        for client in ('claude','codex','grok'):
            self.binding=self.root / (client+'.json')
            self.enroll_native(client)
            before=self.preserved()
            self.assertEqual(self.native(client,self.event(client)), {})
            self.assertEqual(before,self.preserved())

    def test_native_input_deadline_and_worker_deadline_block(self):
        process=subprocess.Popen([sys.executable,'-B',str(HOOK),'native','--client','codex','--timeout','.2'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        # Keep stdin open: ingestion itself must fit in the evaluator budget.
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        start=time.monotonic()
        process.wait(timeout=2)
        self.assertLess(time.monotonic()-start,2)
        self.assertEqual(process.returncode,0)
        self.assertEqual(json.loads(process.stdout.read())['decision'],'block')
        process.stdin.close(); process.stdout.close(); process.stderr.close()
        self.enroll_native('codex')
        bindir=self.root/'slow-bin'; bindir.mkdir()
        shim=bindir/'git'
        shim.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(30)\n')
        shim.chmod(0o755)
        env=dict(os.environ,PATH=str(bindir)+os.pathsep+os.environ['PATH'])
        start=time.monotonic()
        result=subprocess.run([sys.executable,'-B',str(HOOK),'native','--client','codex','--binding',str(self.binding),'--timeout','.3'],
                              input=json.dumps(self.event('codex')),capture_output=True,text=True,env=env,timeout=2)
        self.assertLess(time.monotonic()-start,2)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['decision'],'block')
        self.assertIn('deadline',json.loads(result.stdout)['reason'].lower())

    def test_native_timeout_keeps_deadline_after_group_kill(self):
        self.enroll_native('codex')
        bindir = self.root / 'timeout-bin'
        bindir.mkdir()
        shim = bindir / 'git'
        shim.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(30)\n')
        shim.chmod(0o755)
        # Model a platform rejecting a second signal after a successful group kill.
        injection = self.root / 'signal-boundary'
        injection.mkdir()
        (injection / 'sitecustomize.py').write_text(
            'import os\noriginal = os.killpg\nkilled = set()\n'
            'def killpg(pid, sig):\n'
            '    if pid in killed: raise PermissionError(1, "Operation not permitted")\n'
            '    result = original(pid, sig)\n'
            '    killed.add(pid)\n'
            '    return result\n'
            'os.killpg = killpg\n')
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
                   PYTHONPATH=str(injection))
        result = self.native('codex', self.event('codex'), timeout=1, env=env)
        self.assertEqual(result['decision'], 'block')
        self.assertIn('deadline', result['reason'].lower())

    def test_native_enrollment_cli_and_duplicate_delivery(self):
        result=subprocess.run([sys.executable,'-B',str(HOOK),'enroll','--client','codex','--session-id','session-one',
                               '--binding',str(self.binding),'--repo',str(self.repo),'--state',str(self.state),'--task','one'],
                              capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['decision'],'enrolled')
        self.native('codex',self.event('codex','UserPromptSubmit'))
        result=self.native('codex',self.event('codex'))
        before=self.binding.read_bytes()
        self.assertEqual(self.native('codex',self.event('codex')),result)
        self.assertEqual(self.binding.read_bytes(),before)
        self.binding.write_text('{')
        self.assertEqual(self.native('codex',self.event('codex'))['decision'],'block')

    def test_native_grok_aliases_and_wrong_registration(self):
        self.enroll_native('grok')
        self.accepted()
        for removed in ('hookEventName','hook_event_name'):
            payload=self.event('grok'); del payload[removed]
            self.assertEqual(self.native('grok',payload),{})
        self.assertEqual(self.native('grok',self.event('claude'))['decision'],'block')
        for client in ('claude','codex'):
            self.assertEqual(self.native(client,self.event('grok'))['decision'],'block')

    def test_same_reason_after_tree_change_is_not_unchanged_rejection(self):
        self.enroll_native('codex')
        self.native('codex',self.event('codex'))
        (self.repo/'source').write_text('changed source, still missing checks\n')
        self.native('codex',self.event('codex','UserPromptSubmit',turn_id='synthetic'))
        payload=self.event('codex',turn_id='synthetic',stop_hook_active=True)
        result=self.native('codex',payload)
        self.assertNotIn('Unchanged rejection',result['reason'])
        self.assertEqual(result,self.native('codex',payload))


if __name__ == '__main__':
    unittest.main()
