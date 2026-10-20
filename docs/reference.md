# Barline command and recovery reference

The installed commands are `barline`, `barline-recover` and `barline-hook`. Source-checkout commands `python3 lean.py`, `python3 recover.py` and `python3 hooks/stop.py` remain supported.

## Gate and scope

Local checks execute, log their command/exit/result/environment, and bind evidence to HEAD, branch, index, tracked files and nonignored untracked files. An edit or branch switch makes old checks stale. Source-mutating checks become INCONCLUSIVE. Ignored dependencies/build output and external state are not fingerprinted; report environmental limits and add repo-specific behavioral checks when needed.

Only the latest attempt for each check can qualify, and it must PASS on the current fingerprint. Returning to an earlier tree cannot revive its older pass after a newer failed, blocked or inconclusive attempt. A latest attempt on a different tree is reported in `missing`; rerun that check unchanged on the current tree. Other checks keep their own attempt history.

`assess` is the lead's recorded acceptance judgment, separate from command evidence. A PASS says every configured local check passed on this tree and the lead marked its bar met. It does not certify arbitrary semantics, independent review or a deployed system. Work state (ACTIVE/PAUSED), scope and validation (PASS/FAIL/BLOCKED/INCONCLUSIVE) stay separate.

Optional [completion hooks](completion-hooks.md) reuse a read-only gate observation and distinguish acceptance from explicit non-completion handoffs. Claude Code, Codex CLI and Grok CLI adapters have deterministic process coverage and version-specific native smoke evidence. Native failures and continuation caps can still let a client stop while Barline remains nonpassing. See the guide's validation status before opting in. Adding the examples does not activate them.

### Follow-ups and frozen contracts

The lead compares each follow-up against the current contract. Clarifications that leave acceptance criteria, exclusions and authorized actions unchanged continue the same task without another contract or approval. Material changes require a new frozen task in the existing ledger. Put the actual follow-up authorization reference in the new contract's `owner_request`; an already-authorized user request needs no additional approval.

If a contradiction is identified, declare it immediately, even if the replacement contract is not ready:

```bash
python3 /path/barline/lean.py --state /path/work/session.json assess --task fix --outcome conflict --evidence 'Follow-up request requires input validation excluded by fix'
python3 /path/barline/lean.py --state /path/work/session.json start --task validate --follows fix --repo /path/repo --contract /path/work/validate.json
```

`conflict` is sticky: later assessments cannot remove it or mark that task met. `start --follows` atomically records both task links and supersedes the predecessor. Its contract, check evidence and receipts remain unchanged as history, but its gate cannot PASS again. Reconciliation consists of completing the successor against its accurate new acceptance bar, not rewriting or reopening the old contract. A material follow-up can use `--follows` directly when no interim conflict declaration is needed.

The successor must reference an existing, not already superseded task in the same repository and supply a nonblank `owner_request` different from the predecessor's. It must retain PR mode and all required external obligations. It starts with empty check evidence and receipts, including on an identical tree. Session identity and history remain intact. `init` continues to reject an existing ledger.

These checks enforce the declared lifecycle, not semantic classification. The CLI does not read arbitrary requests, detect contradictions hidden in acceptance prose, prove that a new contract captures the request, or authenticate authorization. An agent that omits the declaration/link can still misuse an old task. Independent review and repository protections remain necessary where that trust boundary is insufficient. Absent conflict/link fields mean no declaration has been recorded, not that scope was independently verified.

For PR delivery set `mode` to `pr`, retain the existing repo requirements and include `repo-ci` and `final-adversarial-review` in `required_external`. Add any required baseline/platform/security lanes. Ordinary `receipt` pointers leave the gate BLOCKED. After actually reading the authoritative CI result or independent review, the lead can add a matching receipt with `--verified-by 'named source actually consulted'`, its actual `--reference` URL/path, `--head` and `--result`. All required receipts must be PASS on the exact candidate, and repo CI requires a clean tree. Edits invalidate them. A later FAIL supersedes earlier success.

External verification is recorded by the lead, not fetched or authenticated by this CLI. The flag is an evidence declaration, not an approval request or security proof. Do not add it to an unverified agent claim. The lead's final report must name the independently observed checks/revision, required review independence, and this automation limit.

The canonical [review requirements](../AGENT.md) remains unchanged. Local work does not trigger it merely because this harness is used. Repo-specific requirements always win. Switching a PR contract to local to evade an obligation is prohibited.

## Delegation and stopping

Only the lead mutates the ledger and owns integration, evidence and completion. [AGENT.md](../AGENT.md) is the canonical delegation decision rule: the lead may choose useful specialists within authorized scope without redundant approval, and keeps small or sequential work direct. Children follow self-contained briefs and do not initialize separate ledgers. Concurrent writers use separate worktrees; read-only children may safely share a checkout. Host permissions and repository rules remain authoritative; the harness does not spawn or supervise specialists.

Use the current client's authoritative task/session records and inspect actual child artifacts and integration state before relying on a report. See the optional [T3 setup guide](../examples/t3/README.md) for T3-specific APIs and runtime limits, and [PRINCIPLES.md](../PRINCIPLES.md) for self-contained, optional decision guidance.

The executable exclusively locks the ledger and writes it atomically. It records a BLOCKED check result before launch and replaces that provisional result only after observing completion. A lead crash therefore cannot expose an older PASS for the same check, even after source restoration. The record includes the process group when known; after a crash, verify and clean up owned work before rerunning. Never blindly signal a recorded PID, which may have been reused. Check timeouts and interruption terminate the owned process group. Check output goes to local log files; log size is not capped.

`hypothesis --task fix --description 'specific causal explanation' --result failed` records a repair attempt. Two failed hypotheses block new work. `park --task fix --next 'unrelated discovery'` records scope exclusions. These declarations are procedural accounting, not automatic detection of what the agent thinks or does.

A checkpoint plus `diagnose --task fix --new-evidence 'new reproducer/source finding'` can reopen work after the repeated-hypothesis stop while retaining its history. Rewording an old hypothesis is not new evidence. Stop when mandatory validation is blocked, and do not expand into speculative repair campaigns.

## Checkpoint and resume

```bash
python3 /path/barline/lean.py --state /path/work/session.json checkpoint --task fix --output /path/work/fix-checkpoint.tar.gz --next 'Inspect failing fixture, then rerun test'
```

The archive contains Git history, the actual nonignored working files (including untracked work), staged patch, ledger, check logs and a short resume note. Deleted files are absent from `worktree/`; recovery replaces the cloned worktree before overlaying the captured files. Named branches, detached HEAD, staged changes, file modes and symlinks retain their fingerprint identity. Submodules/special files are rejected rather than incompletely preserved. Ignored files, credentials, external services and installed dependencies are outside the archive's coverage. Use an adapter if unfinished work depends on them. Tracked/nonignored files are included as-is; preserve only in an authorized destination with the appropriate access.

`log-manifest.json` maps every task/evidence index in the embedded session to its captured log member and SHA-256. Logs from predecessor tasks and other repositories are included; repeated physical files share one payload and colliding basenames have distinct member names. Only the selected task's checkout and Git history are backed up. A missing completed-run log blocks checkpoint creation. A missing unfinished-attempt log is marked unavailable with unknown creation history: the ledger cannot prove whether it was never opened or was later deleted.

Before archive publication, a checkpoint failure leaves the ledger and requested output unchanged. Archive publication and ledger save are separate operations. If hashing or ledger publication fails after the archive is published, the error identifies the recoverable archive path and its hash when known; inspect that archive and the unchanged ledger before deciding how to reconcile them. Do not blindly repeat checkpoint creation at another path.

Child tasks and separate worktrees are outside this archive. Before checkpointing, consult actual client task records and include task IDs/providers, status/evidence, worktree/branch, integration state and remaining work in `--next` or a referenced external handoff. Separately preserve unintegrated artifacts when needed. On resume, inspect the current client's authoritative task/session records and those worktrees before repeating work; do not automatically cancel tasks or delete worktrees. This uses the existing checkpoint format.

Local archives are explicitly **unconfirmed**. Save the archive AND the latest session ledger in an authorized persistent destination. After observing its successful save receipt, record:

```bash
python3 /path/barline/lean.py --state /path/work/session.json durable --task fix --location 'returned persistent file/version identity'
python3 /path/barline/lean.py --state /path/work/session.json resume --task fix --expect RECORDED_FINGERPRINT
```

Persist the finalized ledger as well. The archive's embedded ledger is the pre-receipt recovery record. Restore a trusted checkpoint with the companion `recover.py` installed beside the same version of `lean.py`:

```sh
python3 /path/barline/recover.py /path/fix-checkpoint.tar.gz /path/NEW-recovery-directory --task fix
```

The destination must not exist, and its parent must exist. The helper validates archive paths and member types, creates `repo/`, restores the recorded named branch or detached HEAD, replays the index, and verifies the full fingerprint. It copies the original archive without changing its bytes, restores every available task log using the manifest, and writes a relocated `state.json`. Only the selected task's checkout path is changed; other checkout paths remain historical because their repositories are outside this checkpoint. Contracts, conflicts, check results, external receipts, hypotheses and embedded event history stay intact. Original embedded records remain in `archived-state.json` and `archived-log-manifest.json`. Unavailable unfinished logs remain absent; the recovery result lists them with unknown creation history.

`resume.md` includes the exact executable shell block used by recovery tests. Save that block as a script and pass its NEW destination as argument 1; argument 2 optionally supplies a relocated archive path. Its tool path points to the installed companion; update that path if the harness moved to another machine. Recovery requires Python, Git and this harness installation. It does not install dependencies or establish durability. Read the recovered contract and history, preserve the archive and ledger in an authorized persistent destination, then use `durable` with the real receipt and `resume --expect FINGERPRINT` before continuing.

For older format-2 archives, use the same companion command rather than the old checkout-by-commit instructions. The helper restores the saved branch and index without editing the old archive or its hash. Legacy archives without a manifest are recoverable only when every referenced log is present with unambiguous basenames. Missing logs or an omitted detached commit cannot be reconstructed: recovery stops and explains the missing evidence instead of inventing it. Unsupported format-1 ledgers, special members, missing required members and mismatched restored fingerprints also stop recovery. Existing user work is never overwritten.

## Ledger format and existing sessions

New ledgers use format `version: 2` with `tasks` and `events`. Tasks have no deadline, and sessions have no `limits`, `used_seconds`, `attention_minutes` or `reservations`. Check records contain results, exact-tree identity, logs, timestamps and the process group when available, with no cumulative spending or elapsed-time accounting. `reserve`, `settle`, `attention` and `extend` are removed, as are `--minutes`, `--execution-seconds`, `--attention-minutes` and `--helpers`.

Format-1 ledgers from the budgeted v0.1 implementation are rejected unchanged, including expired ledgers. There is no compatibility mode, automatic migration or reset. Before continuing one, back up the original, inspect held reservations and unfinished checks, and verify/clean up any owned work. A reviewed conversion must retain task IDs, contracts, links, conflicts, evidence, hypotheses, checkpoints and event history in the same session; remove the obsolete session accounting fields and task deadlines, and set the format to 2. An unfinished check needs a BLOCKED result so an earlier PASS is not exposed. Preserve the old ledger as the historical source for removed accounting. Update recovered checkpoint ledgers consistently and re-confirm any changed archive identity. Do not simply change the version number or initialize another ledger to abandon history. No existing session files are converted by this code change.

## What v0.2 controls

| Boundary | Behavior |
| --- | --- |
| Checks executed through this CLI | Allowlisted argv, per-command timeout, exact-tree evidence |
| Agent's normal coding and tool use | Session guidance; no sandbox or interception |
| Delegation | Lead-owned decisions and briefs; client permissions and repository rules apply; see optional [T3 setup](../examples/t3/README.md) for runtime limits |
| Human attention | Task boundaries, evidence and stopping; no numerical accounting |
| CI/publication | No launcher or cost meter; external requirements remain explicit |
| Persistent checkpoint | Archive verified locally, then operator-confirmed save receipt |

The ledger is editable by its owner. This is a workflow guard, not a hostile-agent security boundary. There is no hidden root authority, new orchestrator, billing change or fleet rollout. Notion-authored reusable skills and generated plugins remain their existing source of truth; `AGENT.md` is this executable's session guide, not a published skill. Repo adapters are command/source indexes to refresh in the actual checkout.

Run the meaningful boundary tests from this package:

```bash
python3 -m unittest discover -s tests -v
```

GitHub Actions runs the same full suite for pull requests and pushes to `main`, on macOS and Linux with Python 3.10 and 3.14. See `.github/workflows/ci.yml`. These checks exercise the CLI; fresh T3 conversations remain separate integration validation.

Revise when real work shows a defect or unnecessary friction. No minimum task count, baseline campaign or 30% pass/fail target.
