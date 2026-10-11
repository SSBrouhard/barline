# T3 Code harness entry point

Read `AGENT.md` before working in this project. It is the session guide for the Lean harness.

The installed executable is `lean.py` in this directory. Invoke it with Python 3.10 or newer. Keep session ledgers, contracts and check logs outside this repository, in a separate session directory under the sibling `lean-sessions` folder. Use a distinct ledger for each independent T3 conversation. Continue an existing conversation with its existing ledger rather than silently resetting its budget.

Use the harness for requested work: freeze the task contract, run named checks, assess actual behavior, and report the gate result. Read and retain the target repository's own instructions. The harness accounts for checks and recorded reservations; it does not intercept every T3 tool call or measure model usage.

For the local package check, the command is `python3 -m unittest discover -s tests -v`. The package has no third-party runtime dependencies.

Do not push, publish, merge or change global agent instructions as part of setup. Never add an agent co-author to commits. Never use an em dash. Preserve the bundled original `AGENT.md` and Git history.
