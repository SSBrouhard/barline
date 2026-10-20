# Optional completion guard

Installed use: replace `python3 -B /absolute/path/to/barline/hooks/stop.py` in the examples with the absolute path to your environment's `barline-hook` executable. Find it with `command -v barline-hook`. Keep that environment installed and use its absolute path in native configuration. The source-checkout commands below remain supported.


The shared guard observes one explicitly enrolled native lead session and task. It never runs checks, assesses acceptance, records CI/review receipts, fetches evidence or changes a ledger. Client protocol adapters and opt-in registration are separate from this shared boundary. No configuration is activated by adding these files.

`gate` and `status` retain their existing ledger-writing behavior. Hooks use the new read-only `observe` boundary instead:

```sh
python3 -B /absolute/path/to/lean.py --state /external/session.json observe --task exact-task --timeout 5
```

This prints `decision: observed` plus the repaired gate result, or `decision: block` and an observation error. Exit 0 means an observation succeeded, not that the task passed. Unavailable observation exits 2. A successful observation can contain `validation: INCONCLUSIVE`, `FAIL` or `BLOCKED`.

The evaluator opens the ledger's existing lock without creating it, takes a nonblocking shared lock, validates format 2 and the selected task, and compares full repository snapshots and ledger bytes before returning. Busy or missing locks, malformed evidence, unsupported versions, missing checkouts and changing views block observation. Git optional index refresh is disabled. File contents are stream-hashed with the existing fingerprint encoding, including branch, HEAD, modes, symlink targets and index changes.

## Shared operations

`hooks/stop.py shared --timeout 5` accepts one JSON object on stdin and returns one JSON object on stdout with exit 0. This is the internal shared protocol, not a native client's hook response. Native completion adapters map every `block`, worker failure and timeout to their documented native blocking response. A recognized `UserPromptSubmit` always permits human input, including when turn recording fails; it never grants permission to complete. They must select a deadline below the native hook timeout, reserving startup and response headroom. The supervisor bounds the whole worker, including context/ledger JSON loading, file traversal and Git, and kills its owned process group at the deadline.

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

Lifecycle `turn` receives the current native `turn_id`. It shares observation's locked format, task, evidence-schema and canonical repository validation, including the requirement that context and lock stay outside every target checkout. It does not run Git, traverse source, compute fingerprints or assess acceptance. Enrollment, handoff and Stop evaluation still use full coherent observation. It clears any previous turn's handoff when identity changes. Duplicate delivery of the same identity preserves context bytes and the handoff. Missing native identity clears the old handoff and disables the handoff path; it does not infer identity from transcript text. Turn context is a correlation record, never authorization.

Explicit `handoff` adds `reason` (`progress`, `input` or `external-blocker`) and nonempty `next`. The operation reads the current native turn from the external context itself. A caller-supplied `turn_id` cannot select a different turn. It records that identity, the current fingerprint and ledger-content digest. An agent must use this explicit operation before returning a non-completion handoff; prose in a final response cannot create one. A later turn, changed source or changed ledger invalidates the record. Repeated unchanged Stop observations return the same decision without changing context or acceptance.

Context operations use an existing exclusive nonblocking lock and atomic replacement. Contention asks the caller to retry the same event. Native prompt failures permit input with diagnostic context for Claude/Codex; Grok discards allowed prompt context. Completion failures still block. Only explicit enrollment can create that context lock. All target ledger, log, source and index bytes remain unchanged during shared operations; enrollment, turn, handoff and native rejection diagnostics write only adapter context.

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

The core cannot authenticate lead-recorded acceptance, external verification or handoff reasons. Optional client hooks can also be skipped or defeated by native trust settings, timeouts, crashes and continuation caps. A forced native stop never changes Barline evidence or becomes a PASS. Fixture tests establish the shared boundary only; they do not prove a native client loaded or enforced a hook.

## Native command adapters

`hooks/stop.py native --client claude|codex|grok [--binding /external/context.json] --timeout 5` reads one native event from stdin and prints only native response JSON, with exit 0. The adapter translates complete, valid pause, explicit handoff and not-applicable into `{}`. For completion, it translates nonpass, unavailable evidence, malformed input, identity mismatch, timeout and configuration errors into `{"decision":"block","reason":"..."}`. After native normalization recognizes a valid `UserPromptSubmit`, binding, ledger, lock, identity-validation and worker-deadline failures permit that prompt. Claude/Codex receive diagnostic `additionalContext`; Grok receives `{}` because it discards allowed prompt context. Unknown, malformed or wrong-client envelopes remain blocked. The input/normalization and evaluation workers share one total deadline, so JSON loading, input pipes and subprocess cleanup remain bounded. It never emits `continue: false`, which can override other native blocking hooks. Its evaluator budget includes pipe ingestion, JSON parsing, identity normalization, binding reads, observation and diagnostic context writes. Native registration must allow at least 10 seconds, leaving startup/cleanup/response headroom around the 5-second evaluator deadline.

An omitted binding means the optional guard is not enrolled. An explicitly configured binding must exist and match. No latest-session discovery or automatic enrollment occurs. Native event kind and known child/fork metadata are checked before binding reads, including Codex children that share their parent's `session_id`. Unknown or malformed identity metadata blocks rather than selecting the parent. Child events, teardown, interruption and API-failure events return `{}` as irrelevant, never a completion receipt. Grok Stop is applicable only for `reason: end_turn`; shutdown and channel closure cannot keep a native turn alive.

The identity fields are Claude `session_id`/`prompt_id`, Codex `session_id`/`turn_id`, and Grok `sessionId`/`promptId`. Grok supports its camelCase `hookEventName` with snake_case values and its compatibility `hook_event_name` with PascalCase values; contradictory names block. The Claude and Codex adapters reject Grok envelopes, so loading the wrong compatibility registration cannot silently report acceptance. This is a workflow guard, not an authenticated security boundary. Named custom agents, children and forks with child metadata are deliberately excluded from lead enrollment.

### Explicit enrollment

Choose the exact task and the native lead session's identity from authoritative native session evidence. Create the external parent directory first, then enroll explicitly:

```sh
python3 -B /absolute/path/to/barline/hooks/stop.py enroll \
  --client codex --session-id ACTUAL_NATIVE_SESSION_ID \
  --repo /absolute/target-checkout --state /external/session.json \
  --task exact-task --binding /external/codex-binding.json
```

Use a different external binding for every client/conversation/task. Enrollment will not overwrite an existing binding. It observes the named task and writes only the context and its lock. Enrollment does not run a check or assert acceptance. The command prints shared JSON and exits 2 on error; it is an explicit setup command, not the native hook entry point.

### Current-turn handoffs

Register both `UserPromptSubmit` and `Stop`. The prompt event captures the native current turn. Missing turn identity clears any older handoff and disables creating a handoff until a supported native prompt arrives; it never extracts an ID from prompt prose or a transcript. Claude and Codex receive a ready-to-run handoff command in `hookSpecificOutput.additionalContext`. Grok currently discards allowed `UserPromptSubmit` stdout and context, so its guide and Stop rejection feedback supply that command instead. Grok still captures native turn identity internally. If recording fails, the diagnostic does not advertise the previous handoff as current. A subsequent Stop must match its own native turn ID to both the recorded turn and handoff, so an unrecorded new turn or missing native ID cannot reuse an old handoff. A duplicate of the same native turn deliberately preserves that turn's handoff. Retry the native prompt after recovering access before recording a handoff for the new turn.

For a legitimate input request, progress update or external blocker, use the explicit command before ending the turn:

```sh
python3 -B /absolute/path/to/barline/hooks/stop.py handoff \
  --binding /external/codex-binding.json \
  --reason input --next 'Ask the owner for the missing decision'
```

`--reason` accepts `progress`, `input` or `external-blocker`. The command reads the identity and current turn directly from the chosen binding; agents do not copy IDs out of model text. It records a non-completion handoff bound to that turn, fingerprint and ledger digest. This does not grant permission for any additional work, change the task's acceptance, or create a PASS. New native prompts clear the old handoff. Source or ledger changes invalidate it. If the binding is broken, recover the explicit configuration before recording a handoff; do not bypass the guard by inventing identity.

Repeated unchanged rejection returns stable diagnosis/handoff feedback. Minimal rejection history lives only in the external context and compares the observed fingerprint, ledger digest and blocking reason. A changed tree with the same missing-check reason is not called an unchanged rejection. Documented `stop_hook_active` (or Grok `stopHookActive`) markers retain this history across synthetic continuations, whether a continuation reuses or changes native turn identity; prompt wording is never used. Duplicate delivery is idempotent. A marker never allows completion, records a handoff or resumes repairs automatically. The hook cannot force endless continuation: client cancellation, native caps and failures remain possible, with acceptance unchanged.

Grok's `backgroundTasks` and `sessionCrons` arrays must both be present and empty before the adapter permits a `complete` decision. Missing arrays are unknown, not empty. Account for pending work or scheduled wakeups through a valid pause or explicit non-completion handoff. That handoff preserves the distinction between waiting and accepted completion.

### Optional registration examples

The files under `hooks/examples/` are inactive examples with placeholder absolute paths:

- `claude.json` uses `--client claude` and a Claude-specific external binding.
- `codex.json` uses `--client codex` and a separate Codex binding.
- `grok.json` uses `--client grok` and a separate Grok binding.

Each example registers synchronous `UserPromptSubmit` and `Stop` command hooks with native timeout 10 seconds and evaluator timeout 5 seconds. Replace paths and explicitly enroll the intended session before opting in. Select only the example for the client you intend to run; do not install all examples into shared settings. Review existing hook registrations and use exactly one registration per event for this guard. Duplicated deliveries are harmless to acceptance but can contend on the context lock. A contended prompt remains allowed with a retry diagnostic where the client supports context; a contended Stop blocks.

Grok scans Claude-compatible hook sources by default. If using the Grok example, separately review the user's Grok compatibility configuration and disable Claude hook scanning with `[compat.claude] hooks = false` in the appropriate Grok TOML layer. Do not automatically edit user configuration or trust settings. If Claude-compatible sources remain active, a Claude adapter receiving a Grok event blocks and identifies the mismatch. Registering this guard through multiple native and compatibility sources can produce duplicate feedback. Keep each integration isolated and inspect the client's actual hook inventory before any native support claim.

### Verification boundary

Process fixtures in `tests/fixtures/hooks/` are source-derived examples, not native captures. They verify stdin/stdout/exit translation, read-only target observation, malformed and missing context, child filtering, native identities, handoffs, repeated rejection and deadline behavior. They cannot establish that a particular installed client loads, trusts or enforces its hooks. Native support must be recorded separately with the exact version, launch mode, isolated configuration and observed outcomes. No client configuration is activated by these examples. T3-mediated paths are unverified.

### Native validation status, 2026-10-20

Native loading and Stop behavior were observed in the following exact versions and launch modes using disposable repositories and client profiles/settings. The local suite now has 76 tests: 31 shared/native-protocol tests and 45 core/recovery tests. The original native smoke run accompanied 71 tests; follow-up regressions cover failed prompt recording, worker deadlines and lightweight turn validation. Native prompt-recovery observations are recorded separately. The existing macOS/Linux and Python 3.10/3.14 CI matrix remains unchanged; hosted CI was not invoked for this local work.

| Client | Observed launch | Observed outcomes and limits |
| --- | --- | --- |
| Claude Code 2.1.304 | Print mode with explicit disposable settings, other settings sources excluded and session persistence disabled | Current PASS allowed Stop. Nonpassing evidence blocked repeated stops. An explicit input handoff allowed a question without acceptance; a later native prompt cleared it and blocked. Disabled hooks were inactive. A missing executable (exit 127) and native hook timeout allowed the client to finish. The native block cap ended the turn after its warning of nine consecutive blocks; Barline stayed nonpassing. SIGINT interrupted a later blocked turn. Print mode does not establish interactive workspace or hook trust behavior. |
| Codex CLI 0.163.0 | Interactive TUI with disposable CODEX_HOME, normal project trust and exact two-hook review | Untrusted hooks were skipped. Trusted hooks blocked nonpassing stops, allowed current PASS and allowed an explicit input handoff; a new native prompt cleared the handoff. Native continuations in this tested version retained the same turn_id and set stop_hook_active. Missing-executable and native-timeout failures returned to idle without a Barline PASS. User interruption ended repeated rejection. No native cap was reached in the bounded run. Fresh recovery supported immediate explicit enrollment and a valid native pause with unchanged ledger bytes. |
| Grok CLI 1.0.62 | Interactive PTY with disposable GROK_HOME, normal folder trust and a dedicated leader socket; then headless -p using that isolated trusted profile | Native inventory contained one UserPromptSubmit and one Stop hook, with compatibility hook/MCP discovery disabled. Current PASS allowed Stop. Nonpassing evidence blocked eight times, then the native continuation cap ended the turn with acceptance unchanged. An explicit input handoff allowed a question; the next prompt cleared it and blocked again. An untrusted folder ran no hooks. Missing executables and native timeouts failed open. Interactive cancellation preserved nonacceptance. |

These are observed workflow guards, not guarantees that a client cannot stop. Native failures, trust settings, cancellation and continuation caps can bypass enforcement while the Barline task remains nonpassing. A stopped client, exit 0 or a model's final text is never a Barline completion receipt. Do not raise native caps or install trust bypasses automatically.

The smoke fixtures used explicitly selected existing configured model routes; credentials stayed in process memory/environment. No global/fleet hook activation or trust configuration was changed. Test processes were stopped or exited; local disposable fixtures and sanitized outcome receipts remain under the originating task's work/native-smoke directory. Exact commands, outcomes, recovery rebinding evidence and remaining coverage limits are recorded under [PORT-86](https://linear.app/stephen-portfolio/issue/PORT-86). Interactive Claude trust, other versions/modes, other operating systems and T3-mediated paths remain unverified. Revalidate after native protocol or trust changes, and enroll a fresh binding after recovery.

The protocol sources consulted for this adapter implementation are the official [Claude Code hooks reference](https://code.claude.com/docs/en/hooks), [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks#stop), and [Grok CLI hooks guide](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md). Native timeout/crash/malformed-output behavior can fail open, particularly in Grok. A client stopping despite a guard failure is a protection limit, never evidence that Barline passed.
