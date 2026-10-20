# Working on Barline

Read [AGENT.md](AGENT.md) for the session contract. One lead owns the ledger and integration. Keep ledgers, contracts, check logs and adapter contexts outside every target checkout. Follow the user's current authority and the target repository's instructions.

Run `python3 -B -m unittest discover -s tests -v`. Packaging validation is documented in [docs/releasing.md](docs/releasing.md). Runtime requirements are Python 3.10+, Git and POSIX; build dependencies are separate.

Preserve historical ledgers and the original session guide at [examples/t3/AGENT.legacy.md](examples/t3/AGENT.legacy.md). Do not activate global hooks, publish, merge or change repository visibility without task authority. Never add an agent co-author trailer or use an em dash.
