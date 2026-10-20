# Optional completion guard

The shared guard observes one explicitly enrolled native lead session and task. It never runs checks, assesses acceptance, records CI/review receipts, fetches evidence or changes a ledger. Client protocol adapters and opt-in registration are separate from this shared boundary. No configuration is activated by adding these files.

`gate` and `status` retain their existing ledger-writing behavior. Hooks use the new read-only `observe` boundary instead:

```sh
python3 -B /absolute/path/to/lean.py --state /external/session.json observe --task exact-task --timeout 5
```

This prints `decision: observed` plus the repaired gate result, or `decision: block` and an observation error. Exit 0 means an observation succeeded, not that the task passed. Unavailable observation exits 2. A successful observation can contain `validation: INCONCLUSIVE`, `FAIL` or `BLOCKED`.

The evaluator opens the ledger's existing lock without creating it, takes a nonblocking shared lock, validates format 2 and the selected task, and compares full repository snapshots and ledger bytes before returning. Busy or missing locks, malformed evidence, unsupported versions, missing checkouts and changing views block observation. Git optional index refresh is disabled. File contents are stream-hashed with the existing fingerprint encoding, including branch, HEAD, modes, symlink targets and index changes.

## Shared operations

`hooks/stop.py shared --timeout 5` accepts one JSON object on stdin and returns one JSON object on stdout with exit 0. This is the internal shared protocol, not a native client's hook response. Native adapters must map every `block`, worker failure and timeout to their documented native blocking response. They must select a deadline below the native hook timeout, reserving startup and response headroom. The supervisor bounds the whole worker, including context/ledger JSON loading, file traversal and Git, and kills its owned process group at the deadline.

For Python callers, use `supervise(request, timeout=5)`. `dispatch(request)` is the worker implementation and is not a safe unbounded hook entry point.

All enrolled requests carry these normalized fields:

| Field | Meaning |
| --- | --- |
| `binding` | Explicit absolute adapter-context path outside every target checkout in the ledger |
| `client` | `claude`, `codex` or `grok` |
| `session_id` | Exact native session identity |
| `role` | `lead`, after the native adapter has rejected child/fork/irrelevant events |
| `repo` | Canonical absolute checkout from the native event |
| `turn_id` | Native turn identity where available; required for using a handoff |

A generic `evaluate` request with no binding returns `not-applicable`. An explicitly selected binding that is missing, unreadable, unsupported or mismatched blocks. The evaluator never searches for a latest ledger/task or rebinds to a successor. Distinct conversations sharing a checkout use distinct binding files.

Explicit `enroll` adds `state` (absolute ledger path) and `task` (exact task ID). It verifies the task and creates only the chosen external context and its lock. The parent directory must already exist. It never overwrites an enrollment. Recovery or a new task requires explicit new enrollment, using a new context path. An example normalized request is:

```json
{
  "operation": "enroll",
  "binding": "/external/native-session.json",
  "client": "codex",
  "session_id": "native-session-identity",
  "role": "lead",
  "repo": "/absolute/checkout",
  "state": "/external/session.json",
  "task": "exact-task"
}
```

Lifecycle `turn` receives the current native `turn_id`. It clears any previous turn's handoff when identity changes. Duplicate delivery of the same identity preserves context bytes and the handoff. Missing native identity clears the old handoff and disables the handoff path; it does not infer identity from transcript text. Turn context is a correlation record, never authorization.

Explicit `handoff` adds `reason` (`progress`, `input` or `external-blocker`) and nonempty `next`. The operation reads the current native turn from the external context itself. A caller-supplied `turn_id` cannot select a different turn. It records that identity, the current fingerprint and ledger-content digest. An agent must use this explicit operation before returning a non-completion handoff; prose in a final response cannot create one. A later turn, changed source or changed ledger invalidates the record. Repeated unchanged Stop observations return the same decision without changing context or acceptance.

Context operations use an existing exclusive nonblocking lock and atomic replacement. Contention asks the caller to retry the same event. Only explicit enrollment can create that context lock. All target ledger, log, source and index bytes remain unchanged during shared operations; only explicit enrollment, turn and handoff operations write adapter context.

## Stop decisions

| Observed condition | Shared decision | Meaning |
| --- | --- | --- |
| No optional enrollment | `not-applicable` | No completion claim |
| Missing, invalid, mismatched or unavailable enrolled evidence | `block` | Retry observation or resolve the stated error; no fallback initialization |
| Matching current-turn handoff, fingerprint and ledger digest | `handoff` | Allow this non-completion stop; acceptance stays unchanged |
| PAUSED with matching checkpoint fingerprint, intact archive hash and next action | `pause` | Allow pause; this does not confirm durability or readiness to resume |
| ACTIVE gate PASS, fewer than two failed hypotheses and no conflict/successor | `complete` | The configured acceptance claim passes |
| Other active result | `block` | Continue only authorized validation or explicitly record a handoff |
| Two failed hypotheses | `block` | Stop repairs; checkpoint for diagnosis with new causal evidence or record a handoff |

The core cannot authenticate lead-recorded acceptance, external verification or handoff reasons. Optional client hooks can also be skipped or defeated by native trust settings, timeouts, crashes and continuation caps. A forced native stop never changes Lean evidence or becomes a PASS. Fixture tests establish the shared boundary only; they do not prove a native client loaded or enforced a hook.
