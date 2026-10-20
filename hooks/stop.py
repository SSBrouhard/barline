#!/usr/bin/env python3
"""Optional completion guard shared policy. Native protocol adapters are separate."""
import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys

# Importing the core must not create __pycache__ inside a target checkout.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lean

CLIENTS = ('claude', 'codex', 'grok')
MAX_REQUEST = 1024 * 1024


def blocked(reason):
    return {'decision': 'block', 'reason': str(reason)[:2000]}


def supervise(request, timeout=5):
    """Bound the whole evaluator, including context/ledger JSON, Git and hashing.

    Native callers must choose a deadline below their hook timeout, with headroom
    for import/startup and serializing their native response. No work runs here.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        return blocked('Evaluator timeout must be finite and between 0 and 60 seconds')
    payload = request if isinstance(request, str) else json.dumps(request)
    if len(payload.encode()) > MAX_REQUEST:
        return blocked('Completion request too large')
    process = None
    try:
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '_evaluate'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            # A misbehaving executable may have detached a descendant retaining
            # a pipe. Cleanup must not consume the response serialization budget.
            try:
                process.communicate(timeout=.2)
            except subprocess.TimeoutExpired:
                process.stdout.close()
                process.stderr.close()
            return blocked('Observation deadline exceeded; retry observation or use an explicit handoff after access recovers')
        if process.returncode:
            return blocked('Evaluator unavailable; inspect configuration and retry observation')
        if len(stdout.encode()) > MAX_REQUEST:
            return blocked('Observation response too large; inspect task records before retrying')
        result = json.loads(stdout)
        if not isinstance(result, dict) or 'decision' not in result:
            return blocked('Invalid evaluator response')
        return result
    except (OSError, ValueError, RecursionError) as exc:
        return blocked('Evaluator unavailable: ' + str(exc))
    finally:
        # The evaluator owns this group. Clean any descendant retaining a pipe or
        # outliving its parent, including on interruption of the hook process.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def required_text(value, description):
    lean.require(isinstance(value, str) and bool(value.strip()), description + ' required')
    return value


def absolute(value, description):
    path = Path(required_text(value, description))
    lean.require(path.is_absolute(), description + ' must be absolute')
    return path.resolve()


def outside(path, observation):
    lean.require(not any(path.is_relative_to(Path(repo)) for repo in observation['repos']),
                 'Adapter context and lock must be outside every target checkout')


@contextlib.contextmanager
def context_lock(path, create=False):
    # Only explicit enrollment can create this lock. Subsequent operations never
    # turn a missing enrolled context into a new enrollment.
    with open(str(path) + '.lock', 'a' if create else 'r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise lean.Denied('Adapter context is busy; retry the same event')
        yield


def validate_identity(request, context):
    lean.require(type(context.get('version')) is int and context['version'] == 1,
                 'Unsupported adapter context version')
    for field in ('client', 'session_id', 'role'):
        lean.require(context.get(field) == request.get(field), 'Enrolled ' + field + ' differs from native event')
    lean.require(context['client'] in CLIENTS and context['role'] == 'lead', 'Only the enrolled native lead is applicable')
    required_text(context['session_id'], 'Native session identity')
    lean.require(absolute(request.get('repo'), 'Native checkout') == absolute(context.get('repo'), 'Enrolled checkout'),
                 'Native checkout differs from enrollment')
    required_text(context.get('task'), 'Exact enrolled task')
    absolute(context.get('state'), 'Enrolled ledger')
    lean.require('turn_id' in context and (context['turn_id'] is None or
                 isinstance(context['turn_id'], str) and bool(context['turn_id'].strip())),
                 'Invalid native turn context')
    lean.require('handoff' in context and (context['handoff'] is None or isinstance(context['handoff'], dict)),
                 'Invalid handoff context')
    if context['handoff'] is not None:
        handoff = context['handoff']
        lean.require(handoff.get('reason') in ('progress', 'input', 'external-blocker') and
                     isinstance(handoff.get('identity'), dict), 'Invalid handoff record')
        for field in ('next', 'turn_id', 'fingerprint', 'ledger_digest'):
            required_text(handoff.get(field), 'Handoff ' + field)


def binding_identity(context):
    return {key: context[key] for key in ('client', 'session_id', 'role', 'repo', 'state', 'task')}


def decision(context, observation, turn_id):
    handoff = context.get('handoff')
    if isinstance(handoff, dict) and handoff.get('identity') == binding_identity(context) and turn_id and turn_id == context.get('turn_id') == handoff.get('turn_id') and all(
            handoff.get(key) == observation[key] for key in ('fingerprint', 'ledger_digest')):
        return {'decision': 'handoff', 'reason': handoff['reason'], 'next': handoff['next'],
                'validation': observation['validation'], 'task': observation['task']}
    details = {key: observation[key] for key in
               ('validation', 'task', 'missing', 'failed', 'blocked', 'inconclusive', 'reasons')}
    if observation['work_state'] == 'PAUSED':
        if observation['pause_valid']:
            return dict(details, decision='pause', reason='Matching checkpoint and next action recorded; acceptance unchanged',
                        next=observation['next'])
        return dict(details, decision='block', reason='Paused task needs an intact matching checkpoint and next action')
    if observation['failed_hypotheses'] >= 2:
        return dict(details, decision='block', reason='Two failed repair hypotheses: stop repairs, checkpoint for diagnosis with new causal evidence, or explicitly record a non-completion handoff')
    if observation['validation'] == 'PASS':
        return dict(details, decision='complete', reason='Configured acceptance passes on the observed tree')
    outstanding = [name + ': ' + ', '.join(observation[name]) for name in
                   ('missing', 'failed', 'blocked', 'inconclusive') if observation[name]]
    outstanding += observation['reasons']
    return dict(details, decision='block', reason=('; '.join(outstanding) +
        '. Continue only already-authorized validation or explicitly record a progress, input or external-blocker handoff.')[:2000])


def dispatch(request):
    """Internal worker API. Native event filtering belongs before this boundary."""
    lean.require(isinstance(request, dict), 'Completion request must be an object')
    operation = request.get('operation', 'evaluate')
    if operation == 'observe':
        result = lean.observe(absolute(request.get('state'), 'Ledger'), required_text(request.get('task'), 'Exact task'))
        return dict(result, decision='observed')
    lean.require(operation in ('enroll', 'turn', 'handoff', 'evaluate'), 'Unknown shared operation')
    if request.get('binding') is None:
        lean.require(operation == 'evaluate', 'Explicit binding path required')
        return {'decision': 'not-applicable', 'reason': 'No optional enrollment selected'}
    path = absolute(request['binding'], 'Binding path')
    if operation == 'enroll':
        lean.require(request.get('client') in CLIENTS and request.get('role') == 'lead', 'Explicit native lead enrollment required')
        session = required_text(request.get('session_id'), 'Native session identity')
        repo = absolute(request.get('repo'), 'Checkout')
        state = absolute(request.get('state'), 'Ledger')
        task = required_text(request.get('task'), 'Exact task')
        observation = lean.observe(state, task, repo)
        outside(path, observation)
        outside(Path(str(path) + '.lock').resolve(), observation)
        lean.require(not path.exists(), 'Binding exists; preserve it or explicitly select a new binding')
        # The caller chooses the existing external parent; no hidden context root.
        with context_lock(path, create=True):
            lean.require(not path.exists(), 'Binding already enrolled')
            lean.save(path, {'version': 1, 'client': request['client'], 'session_id': session,
                'role': 'lead', 'repo': str(repo), 'state': str(state), 'task': task,
                'turn_id': None, 'handoff': None})
        return {'decision': 'enrolled', 'binding': str(path), 'task': task}
    # Read once before taking a lock to reject a moved context that would cause a
    # lock open inside source. Then re-read under its existing exclusive lock.
    initial = json.loads(path.read_bytes())
    validate_identity(request, initial)
    observation = lean.observe(initial['state'], initial['task'], initial['repo'])
    outside(path, observation)
    outside(Path(str(path) + '.lock').resolve(), observation)
    with context_lock(path):
        context = json.loads(path.read_bytes())
        lean.require(context == initial, 'Adapter context changed; retry the event')
        validate_identity(request, context)
        if operation == 'turn':
            turn = request.get('turn_id')
            lean.require(turn is None or isinstance(turn, str), 'Invalid native turn identity')
            turn = turn if turn and turn.strip() else None
            if context.get('turn_id') != turn:
                context.update(turn_id=turn, handoff=None)
                lean.save(path, context)
            return {'decision': 'context-updated', 'handoff_available': turn is not None}
        if operation == 'handoff':
            # Read identity only from native context; caller-supplied turn IDs do
            # not authorize a handoff or replace the current prompt identity.
            turn = required_text(context.get('turn_id'), 'Current native turn identity')
            reason = request.get('reason')
            lean.require(reason in ('progress', 'input', 'external-blocker'), 'Handoff reason must be progress, input or external-blocker')
            next_action = required_text(request.get('next'), 'Handoff next action')
            context['handoff'] = {'reason': reason, 'next': next_action, 'turn_id': turn,
                'identity': binding_identity(context),
                'fingerprint': observation['fingerprint'], 'ledger_digest': observation['ledger_digest']}
            lean.save(path, context)
            return {'decision': 'handoff-recorded', 'reason': reason, 'next': next_action,
                    'task': context['task']}
        return decision(context, observation, request.get('turn_id'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('shared', '_evaluate'))
    parser.add_argument('--timeout', type=float, default=5,
                        help='Evaluator deadline, below the native hook timeout')
    args = parser.parse_args(argv)
    raw = sys.stdin.read(MAX_REQUEST + 1)
    if len(raw.encode()) > MAX_REQUEST:
        result = blocked('Completion request too large')
    elif args.operation == 'shared':
        # JSON parsing is deliberately inside the deadline-supervised worker.
        result = supervise(raw, args.timeout)
    else:
        try:
            result = dispatch(json.loads(raw))
        except Exception as exc:
            result = blocked('Observation unavailable: ' + str(exc))
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
