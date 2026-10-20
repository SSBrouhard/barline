# Barline

**Keep an agent's “done” tied to the work it actually checked.**

Coding agents can report success after checking the wrong files, reusing an old pass or losing the evidence needed to resume. Barline records a task's agreed outcome, runs named checks and ties their results to the exact Git checkout. Edit a file or switch branches and the earlier pass becomes stale.

One lead owns the task and its ledger; specialist agents can help. Optional completion hooks let Claude Code, Codex CLI and Grok CLI consult the same gate before finishing.

Experimental software, formerly Lean Harness. Python 3.10+, Git and macOS or Linux are required; use WSL on Windows. No third-party runtime dependencies or services.

## Install

Install from [PyPI](https://pypi.org/project/barline/) in a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install barline
barline --help
```

To install from source instead:

```sh
git clone https://github.com/SSBrouhard/barline.git
cd barline
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
barline --help
```

You can also run `python3 lean.py` directly from a source checkout without installing.

## Two-minute demonstration

Run this after installation in a POSIX shell. Everything lives in a new temporary directory, with the ledger outside the demonstration repository.

```sh
demo=$(mktemp -d)
mkdir "$demo/repo"
git -C "$demo/repo" init -q
git -C "$demo/repo" config user.name 'Barline Demo'
git -C "$demo/repo" config user.email 'demo@example.invalid'
printf 'ready\n' > "$demo/repo/source.txt"
git -C "$demo/repo" add source.txt
git -C "$demo/repo" commit -qm 'Initial demo'
cat > "$demo/contract.json" <<'JSON'
{
  "goal": "Keep source.txt ready.",
  "owner_request": "The README demonstration.",
  "mode": "local",
  "non_goals": ["Publication"],
  "must_happen": ["source.txt contains ready followed by a newline"],
  "must_never": ["Use a stale check result"],
  "policy_sources": ["This demonstration"],
  "checks": {
    "content": {
      "argv": ["git", "diff", "--exit-code", "HEAD", "--", "source.txt"],
      "timeout_seconds": 10,
      "expected_exit": 0
    }
  },
  "required_external": []
}
JSON
barline --state "$demo/session.json" init
barline --state "$demo/session.json" start --task demo --repo "$demo/repo" --contract "$demo/contract.json"
barline --state "$demo/session.json" run --task demo --check content
barline --state "$demo/session.json" assess --task demo --outcome met --evidence 'Read source.txt: ready followed by newline'
barline --state "$demo/session.json" gate --task demo
# PASS, exit 0
printf 'changed\n' > "$demo/repo/source.txt"
barline --state "$demo/session.json" gate --task demo
# INCONCLUSIVE, exit 1; content is missing for this checkout
```

An unsuccessful newer check also prevents an older pass from reviving if the source returns to its old contents. A check that changes its own source is inconclusive.

For real work, give your agent [AGENT.md](https://github.com/SSBrouhard/barline/blob/main/AGENT.md), your request and the target repository's instructions. Start from [contract.example.json](https://github.com/SSBrouhard/barline/blob/main/contract.example.json); replace the outcome and checks with meaningful acceptance for that task. Allowed commands can have side effects, so inspect them before running.

## Continue from here

- [Checks, linked tasks, checkpoints and recovery](https://github.com/SSBrouhard/barline/blob/main/docs/reference.md)
- [Optional completion hook setup and tested client limits](https://github.com/SSBrouhard/barline/blob/main/docs/completion-hooks.md)
- [Optional T3 guide](https://github.com/SSBrouhard/barline/blob/main/examples/t3/README.md) and [npm contract example](https://github.com/SSBrouhard/barline/blob/main/examples/contracts/npm.json)
- [Building and publishing](https://github.com/SSBrouhard/barline/blob/main/docs/releasing.md)

From a source checkout, run the regression suite with `python3 -B -m unittest discover -s tests -v`. CI runs it on macOS/Linux with Python 3.10/3.14 and verifies installed distributions.

## Limits

Barline is a workflow guard for cooperative agents. The owner can edit the ledger; acceptance judgments and CI/review receipts are lead-recorded declarations. It fingerprints HEAD, branch, index, tracked files and nonignored untracked files. Ignored files, dependencies and external services need their own checks.

Hooks reduce premature completion when the client loads and runs them. Native trust settings, missing executables, timeouts, crashes and continuation caps can still let a client stop without a Barline PASS. Explicit pauses and handoffs are not acceptance. Native support claims are limited to the versions and launch modes documented in the hook guide.

A checkpoint captures the selected checkout and the session's available logs. It does not capture other child worktrees or prove persistent storage. Usage beyond the original author's workflow remains limited.

MIT licensed. See [LICENSE](https://github.com/SSBrouhard/barline/blob/main/LICENSE).
