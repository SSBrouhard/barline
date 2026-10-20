# Optional T3 setup

Barline works without T3. For a T3 project, read the generic root `AGENT.md` and keep one lead-owned ledger outside the target checkout. Copy `t3.json` into a project only if its local-environment default fits that project.

The preserved `AGENT.legacy.md` records the original Lean/T3 session guide for provenance. It is historical guidance, not an additional dependency or current project policy. Machine-specific startup files and private policy repositories are not required.

When delegating in T3, discover capabilities through `orchestrator_capabilities`, delegate through `delegate_task`, and retain task IDs. Inspect `task_status`, durable `t3_thread_read` records and actual artifacts before integration. Worktree children start from committed state. A runtime-mode ceiling does not enforce a read-only brief or prohibit further delegation.

Only the lead updates Barline's ledger. Include live child IDs, providers, worktrees, integration state and next actions in a checkpoint handoff; preserve unintegrated artifacts separately. No T3 completion hook or fleet rollout is supplied here.
