# Lean Agentic Engineering harness v0.1

A usable lead-agent harness for real work, with optional bounded help, a frozen task contract, exact-tree evidence and durable checkpoints. No pilot prerequisite, service, plugin installation, watcher, dependency install, network call or automatic Git publication.

Requires Python 3.10+, Git, macOS or Linux. Windows needs WSL. Run the file directly with Python; no package manager needed.

## Start using it

Give your coding agent `AGENT.md` and your actual task. The agent fills the short contract from your request and current repo policy. You do not need to operate the ledger yourself.

For direct use, keep the harness and state outside the target repository. Edit a copy of `contract.example.json` with actual checks and behavior. Paths below are examples:

```bash
python3 /path/lean-harness/lean.py --state /path/work/session.json init --minutes 90 --execution-seconds 1800 --attention-minutes 10 --helpers 0
python3 /path/lean-harness/lean.py --state /path/work/session.json start --task fix --repo /path/repo --contract /path/work/fix.json --minutes 30
python3 /path/lean-harness/lean.py --state /path/work/session.json run --task fix --check test
python3 /path/lean-harness/lean.py --state /path/work/session.json assess --task fix --outcome met --evidence 'Actual observed behavior and evidence pointer'
python3 /path/lean-harness/lean.py --state /path/work/session.json gate --task fix
```

`init` is once per session, `start` once per task. A, B and C share the same session budget. Check `argv` runs without shell expansion. Inspect the actual scripts first: an allowed npm/Python command can still have side effects. Dependency preparation is an independently authorized step; the harness never installs dependencies for you.

Use the optional `attention` command to record rough human minutes only when useful. It is not a timer for the user. Execution seconds measure checks plus lead-recorded helper usage; deadlines measure elapsed wall time. Host model tokens and CI costs are not measured here. `--helpers 0` is the default; choose an authorized ceiling for the task/session.

## Gate and scope

Local checks execute, log their command/exit/result/environment, and bind evidence to HEAD, branch, index, tracked files and nonignored untracked files. An edit or branch switch makes old checks stale. Source-mutating checks become INCONCLUSIVE. Ignored dependencies/build output and external state are not fingerprinted; report environmental limits and add repo-specific behavioral checks when needed.

`assess` is the lead's recorded acceptance judgment, separate from command evidence. A PASS says every configured local check passed on this tree and the lead marked its bar met. It does not certify arbitrary semantics, independent review or a deployed system. Work state (ACTIVE/PAUSED), scope and validation (PASS/FAIL/BLOCKED/INCONCLUSIVE) stay separate.

For PR delivery set `mode` to `pr`, retain the existing repo requirements and include `repo-ci` and `final-adversarial-review` in `required_external`. Add any required baseline/platform/security lanes. Ordinary `receipt` pointers leave the gate BLOCKED. After actually reading the authoritative CI result or independent review, the lead can add a matching receipt with `--verified-by 'named source actually consulted'`, its actual `--reference` URL/path, `--head` and `--result`. All required receipts must be PASS on the exact candidate, and repo CI requires a clean tree. Edits invalidate them. A later FAIL supersedes earlier success.

External verification is recorded by the lead, not fetched or authenticated by this CLI. The flag is an evidence declaration, not an approval request or security proof. Do not add it to an unverified agent claim. The lead's final report must name the independently observed checks/revision, required review independence, and this automation limit.

The canonical [PR Freeze Bar](https://github.com/SSBrouhard/Agent-Skills/blob/main/plugins/pr-freeze-bar/skills/pr-freeze-bar/SKILL.md) remains unchanged. Local work does not trigger it merely because this harness is used. Repo-specific requirements always win. Switching a PR contract to local to evade an obligation is prohibited.

## Delegation and stopping

Only the lead mutates the ledger. `reserve --id helper1 --role investigator --seconds 300` reserves from the same shared pool before dispatch. Other helpers/checks cannot spend that reservation. `settle --id helper1 --seconds 240` charges actual usage and releases unused time. A helper overrun is honestly charged. Role is flexible; children receive no publication authority. Helper ceiling counts all session helper invocations, not only concurrently active ones.

The executable locks the ledger, atomically persists reservations before execution, rejects insufficient allowances, and terminates its owned check process group at timeout. A lead crash can leave a held reservation. Verify that owned work stopped, then settle it; do not reset the session. Check output is captured in bounded-duration local log files, not streamed into agent context. Log size itself is not capped in v0.1.

`hypothesis --task fix --description 'specific causal explanation' --result failed` records a repair attempt. Two failed hypotheses block new work. `park --task fix --next 'unrelated discovery'` records scope exclusions. These declarations are procedural accounting, not automatic detection of what the agent thinks or does.

`extend --task fix --minutes 30 --execution-seconds 300 --owner-request 'reference to actual owner approval'` extends a phase without erasing evidence, usage or failures. This field records authority; it cannot establish it. A checkpoint plus `diagnose --task fix --new-evidence 'new reproducer/source finding'` can reopen the hypothesis allowance while retaining all history. Rewording an old hypothesis is not new evidence. Attention limits have no automatic extension.

## Checkpoint and resume

```bash
python3 /path/lean-harness/lean.py --state /path/work/session.json checkpoint --task fix --output /path/work/fix-checkpoint.tar.gz --next 'Inspect failing fixture, then rerun test'
```

The archive contains Git history, the actual nonignored working files (including untracked work), staged patch, ledger, check logs and a short resume note. Deleted files are absent from `worktree/`; recovery instructions explain removal before overlay. Submodules/special files are rejected rather than incompletely preserved. Ignored files, credentials, external services and installed dependencies are outside the archive's coverage. Use an adapter if unfinished work depends on them. Tracked/nonignored files are included as-is; preserve only in an authorized destination with the appropriate access.

Local archives are explicitly **unconfirmed**. Save the archive AND the latest session ledger in an authorized persistent destination. After observing its successful save receipt, record:

```bash
python3 /path/lean-harness/lean.py --state /path/work/session.json durable --task fix --location 'returned persistent file/version identity'
python3 /path/lean-harness/lean.py --state /path/work/session.json resume --task fix --expect RECORDED_FINGERPRINT
```

Persist the finalized ledger as well. The archive's embedded ledger is the pre-receipt recovery record; on full restoration update only physical repo/log/archive paths and the archive hash, re-confirm durable storage, and preserve counters/contracts. Never extract over existing user work. `resume.md` describes clone/overlay/index restoration into a new directory. Compare the fingerprint before continuing. An expired phase needs a recorded owner-authorized extension, not a silent fresh budget.

## What v0.1 controls

| Boundary | Behavior |
| --- | --- |
| Checks executed through this CLI | Allowlisted argv, shared reservations, process timeout, exact-tree evidence |
| Agent's normal coding and tool use | Session guidance; no sandbox or interception |
| Delegation | Lead reservation ledger; host must bound child runtime |
| Human attention | Optional estimates and threshold; not automatic measurement |
| CI/publication | No launcher or cost meter; external requirements remain explicit |
| Persistent checkpoint | Archive verified locally, then operator-confirmed save receipt |

The ledger is editable by its owner. This is a workflow guard, not a hostile-agent security boundary. There is no hidden root authority, new orchestrator, billing change or fleet rollout. Notion-authored reusable skills and generated plugins remain their existing source of truth; `AGENT.md` is this executable's session guide, not a published skill. Repo adapters are command/source indexes to refresh in the actual checkout.

Run the meaningful boundary tests from this package:

```bash
python3 -m unittest discover -s tests -v
```

Revise when real work shows a defect or unnecessary friction. No minimum task count, baseline campaign or 30% pass/fail target.
