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
import select
import shlex
import time
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


def supervise(request, timeout=5, worker='_evaluate'):
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
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), worker],
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
        return dict(decision(context, observation, request.get('turn_id')),
                    fingerprint=observation['fingerprint'], ledger_digest=observation['ledger_digest'])



IRRELEVANT = {'SessionStart', 'SessionEnd', 'SubagentStart', 'SubagentStop',
              'SubagentEnd', 'Interrupt', 'StopFailure', 'StopCancelled',
              'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Notification'}
GROK_EVENTS = {'stop': 'Stop', 'user_prompt_submit': 'UserPromptSubmit',
               **{''.join(('_' + c.lower()) if c.isupper() else c for c in name).lstrip('_'): name
                  for name in IRRELEVANT}}


def normalize(event, client):
    """Reject irrelevant/child events before touching any selected binding."""
    lean.require(isinstance(event, dict), 'Native input must be an object')
    if client == 'grok':
        camel = event.get('hookEventName')
        snake = event.get('hook_event_name')
        name = GROK_EVENTS.get(camel) if camel is not None else snake
        lean.require(name is not None and (snake is None or snake == name),
                     'Unknown or contradictory Grok event names')
    else:
        lean.require(not any(key in event for key in ('sessionId', 'promptId', 'hookEventName')),
                     'Wrong client adapter: Grok envelopes require --client grok and isolated registration')
        name = required_text(event.get('hook_event_name'), 'Native event kind')
    if name in IRRELEVANT:
        return None
    lean.require(name in ('Stop', 'UserPromptSubmit'), 'Unsupported native event kind')
    for key in ('agent_id', 'agent_type', 'subagentType', 'subagent_type',
                'parent_session_id', 'parentSessionId', 'parent_thread_id'):
        value = event.get(key)
        lean.require(value is None or isinstance(value, str), 'Ambiguous child metadata: ' + key)
        if value:
            return None
    for key in ('is_subagent', 'isSubagent', 'forked', 'is_fork', 'isFork'):
        value = event.get(key)
        lean.require(value is None or type(value) is bool, 'Ambiguous child/fork metadata: ' + key)
        if value:
            return None
    if client == 'grok' and name == 'Stop':
        reason = event.get('reason')
        if reason in ('channel_closed', 'shutdown', 'user_interrupt', 'permission_rejected',
                      'permission_cancelled', 'max_turns', 'no_progress', 'api_error'):
            return None
        lean.require(reason == 'end_turn', 'Grok Stop requires reason=end_turn; unknown end state is not completion')
    session_key, turn_key = ('sessionId', 'promptId') if client == 'grok' else (
        'session_id', 'prompt_id' if client == 'claude' else 'turn_id')
    session = required_text(event.get(session_key), 'Native session identity')
    repo = required_text(event.get('cwd'), 'Native checkout')
    lean.require(Path(repo).is_absolute(), 'Native checkout must be absolute')
    turn = event.get(turn_key)
    lean.require(turn is None or isinstance(turn, str), 'Native turn identity must be text')
    turn = turn if turn and turn.strip() else None
    marker = event.get('stopHookActive' if client == 'grok' else 'stop_hook_active', False)
    lean.require(type(marker) is bool, 'Invalid native stop continuation marker')
    return name, {'client': client, 'session_id': session, 'role': 'lead',
                  'repo': repo, 'turn_id': turn}, marker


def handoff_command(binding):
    return shlex.join([sys.executable, '-B', str(Path(__file__).resolve()), 'handoff',
                       '--binding', str(binding), '--reason', 'input', '--next', 'Describe the next authorized action'])


def native_dispatch(request):
    event = json.loads(request['payload'])
    client = request['client']
    normalized = normalize(event, client)
    if normalized is None:
        return {}
    name, identity, marker = normalized
    identity['binding'] = request.get('binding')
    if identity['binding'] is None:
        return {}
    # Explicitly selected contexts must exist and match even for prompt events.
    result = dispatch(dict(identity, operation='turn' if name == 'UserPromptSubmit' else 'evaluate'))
    command = handoff_command(identity['binding'])
    if name == 'UserPromptSubmit':
        if result['decision'] == 'block':
            return result
        if client == 'grok':
            # Grok discards allowed UserPromptSubmit stdout/context.
            return {}
        return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext':
                'Lean optional completion guard is enrolled. For an explicit non-completion handoff run: '
                + command + '. Choose progress, input or external-blocker; do not copy native IDs from text. '
                'The command reads current turn identity from the binding. Continue only already-authorized work.'}}
    if result['decision'] in ('complete', 'pause', 'handoff', 'not-applicable'):
        if client == 'grok' and result['decision'] == 'complete':
            for field in ('backgroundTasks', 'sessionCrons'):
                lean.require(field in event and isinstance(event[field], list),
                             'Grok ' + field + ' unavailable; do not assume no pending work')
                lean.require(not event[field], 'Grok pending ' + field +
                             ': account for background work and use an explicit non-completion handoff')
        return {}
    lean.require(result['decision'] == 'block', 'Unexpected shared completion decision')
    # Persist only minimal diagnostic context. Continuation markers never allow
    # a stop or create a handoff. Preserve history across Codex synthetic turns.
    path = absolute(identity['binding'], 'Binding path')
    with context_lock(path):
        context = json.loads(path.read_bytes())
        validate_identity(identity, context)
        signature = {field: result[field] for field in ('fingerprint', 'ledger_digest', 'reason')}
        previous = context.get('rejection')
        lean.require(previous is None or isinstance(previous, dict), 'Invalid rejection history')
        duplicate = previous is not None and all((previous.get('signature') == signature,
            previous.get('turn_id') == identity['turn_id'], previous.get('marker') == marker))
        repeated = previous.get('repeated', False) if duplicate else bool(
            marker and previous is not None and previous.get('signature') == signature)
        history = {'signature': signature, 'turn_id': identity['turn_id'],
                   'marker': marker, 'repeated': repeated}
        if previous != history:
            context['rejection'] = history
            lean.save(path, context)
    feedback = (' Unchanged rejection after native continuation: stop repairs and diagnose before more work, '
                if repeated else ' Continue only already-authorized validation, ')
    return {'decision': 'block', 'reason': (result['reason'].replace('missing:', 'missing or stale checks:')[:1200]
            + feedback + 'or record an explicit non-completion handoff: '
            + command + '. A continuation marker never means acceptance passed.')[:9000]}



def command_dispatch(request):
    if request['operation'] == 'handoff-cli':
        path = absolute(request.get('binding'), 'Binding path')
        context = json.loads(path.read_bytes())
        identity = binding_identity(context)
        return dispatch(dict(identity, operation='handoff', binding=str(path),
                             reason=request.get('reason'), next=request.get('next')))
    return dispatch(request)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def read_bounded(timeout):
    """Bound native pipe ingestion as well as parsing/observation."""
    deadline = time.monotonic() + timeout
    chunks, size = [], 0
    while True:
        remaining = deadline - time.monotonic()
        lean.require(remaining > 0, 'Native input deadline exceeded')
        ready, _, _ = select.select([sys.stdin.fileno()], [], [], remaining)
        lean.require(ready, 'Native input deadline exceeded')
        data = os.read(sys.stdin.fileno(), min(65536, MAX_REQUEST + 1 - size))
        if not data:
            return b''.join(chunks).decode('utf-8'), max(.001, deadline - time.monotonic())
        chunks.append(data)
        size += len(data)
        lean.require(size <= MAX_REQUEST, 'Completion request too large')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    native = bool(argv and argv[0] == 'native')
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument('operation', choices=('shared', '_evaluate', '_native', '_command', 'native', 'enroll', 'handoff'))
        parser.add_argument('--timeout', type=float, default=5, help='Evaluator deadline, below native hook timeout')
        parser.add_argument('--client', choices=CLIENTS)
        parser.add_argument('--binding')
        parser.add_argument('--session-id')
        parser.add_argument('--repo')
        parser.add_argument('--state')
        parser.add_argument('--task')
        parser.add_argument('--reason', choices=('progress', 'input', 'external-blocker'))
        parser.add_argument('--next')
        args = parser.parse_args(argv)
        lean.require(math.isfinite(args.timeout) and 0 < args.timeout <= 60, 'Evaluator timeout must be between 0 and 60 seconds')
        if args.operation == 'native':
            lean.require(args.client in CLIENTS, 'Explicit --client required')
            # Native examples use timeout=10, leaving five seconds of headroom.
            lean.require(args.timeout <= 5, 'Native evaluator deadline must be at most 5 seconds; configure native timeout 10 or greater')
            raw, remaining = read_bounded(args.timeout)
            result = supervise({'payload': raw, 'client': args.client, 'binding': args.binding}, remaining, '_native')
            output = result.get('output') if result.get('decision') == 'native' else result
            lean.require(isinstance(output, dict), 'Invalid native response')
            result = output
        elif args.operation in ('enroll', 'handoff'):
            request = {'operation': 'enroll' if args.operation == 'enroll' else 'handoff-cli',
                       'binding': args.binding, 'client': args.client, 'session_id': args.session_id,
                       'role': 'lead', 'repo': args.repo, 'state': args.state, 'task': args.task,
                       'reason': args.reason, 'next': args.next}
            result = supervise(request, args.timeout, '_command')
        else:
            raw = sys.stdin.read(MAX_REQUEST + 1)
            lean.require(len(raw.encode()) <= MAX_REQUEST, 'Completion request too large')
            if args.operation == 'shared':
                result = supervise(raw, args.timeout)
            elif args.operation == '_native':
                result = {'decision': 'native', 'output': native_dispatch(json.loads(raw))}
            elif args.operation == '_command':
                result = command_dispatch(json.loads(raw))
            else:
                result = dispatch(json.loads(raw))
    except Exception as exc:
        result = blocked('Observation unavailable: ' + str(exc))
    print(json.dumps(result))
    # Native protocol failures must be explicit blocks with exit 0. Lean's
    # ordinary CLI exit 1 would fail open in several clients.
    return 0 if native or result.get('decision') != 'block' or (argv and argv[0].startswith('_')) or (argv and argv[0] == 'shared') else 2


if __name__ == '__main__':
    raise SystemExit(main())
