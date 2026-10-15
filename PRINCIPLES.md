# Principle index

Detailed principles come from the pstack checkout pinned in agent-fleet-config `sources/skills.json` (`cursor/plugins` at `11713c683fe4add4fd403ea880580abbc9e0875f`). Resolve `<pstack-root>` from the current machine's T3-START.md. Each principle lives at `<pstack-root>/pstack/skills/principle-<name>/SKILL.md`. Do not copy them into this repo or link them into skill discovery roots.

Read a principle's file in full only when its trigger fires in the current task. Cite a principle only if you read its file this session. In the final report only, name each applied principle and the decision it changed. Keep actual behavioral evidence in `assess --evidence`; principle citations do not replace it. A citation that changed no decision is not an application.

AGENT.md, the frozen contract and repository policy outrank every principle file. Scope notes below further restrict principle use; they never override those higher-priority sources. If a pinned principle file is unavailable, continue under existing policies and report a material limitation when necessary. Never silently substitute unpinned upstream content.

## Contract (before `start`)

- `redesign-from-first-principles`: a new requirement lands on an existing design.
- `foundational-thinking`: the contract needs new core types or data structures.
- `experience-first`: the contract involves a product or UX scope tradeoff.
- `exhaust-the-design-space`: no precedent in the repo. Build prototypes only if the contract authorizes them.

## Implementation

- `laziness-protocol`, `subtract-before-you-add`: sizing any diff. Removals stay inside the contract's scope; park the rest.
- `minimize-reader-load`: code under change is hard to trace.
- `model-the-domain`, `type-system-discipline`: stateful or heavily branching logic, typed signatures.
- `boundary-discipline`: validation, error handling, adapters.
- `make-operations-idempotent`: commands or loops that must survive retry or crash.
- `separate-before-serializing-shared-state`: concurrent writers to one file, branch or key.
- `migrate-callers-then-delete-legacy-apis`, `outcome-oriented-execution`: replacing an internal API or a planned migration.
- `build-the-lever`: a script would do or prove the work. Keep it outside the repo unless the contract asks for it.

## Checks and assess

- `prove-it-works`: before `assess --outcome met`.
- `sequence-verifiable-units`: multi-unit work. One named check per unit.
- `test-behavior-not-implementation`: writing, changing or keeping a test.
- `explain-the-number`: any measured number in evidence.
- `fix-root-causes`: before recording a `hypothesis`.
- `attack-the-premise`: at the two-failed-hypothesis stop, before `diagnose`.

## Delegation and follow-ups

- `guard-the-context-window`: bulk reading, long logs, or fan-out that would crowd the lead's context.
- `never-block-on-the-human`: reversible work inside the frozen contract only. Material scope changes, conflicts and the stop conditions in AGENT.md still go to the owner.
- `encode-lessons-in-structure`: the same correction given twice. AGENT.md's lesson rule still applies: no lesson by default.
