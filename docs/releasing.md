# Build and publish Barline

The distribution name is `barline`; commands are `barline`, `barline-recover` and `barline-hook`. Releases use GitHub Actions and PyPI Trusted Publishing.

## Local build and installed checks

Use a disposable environment and keep build output outside the checkout:

```sh
python3 -m venv /tmp/barline-build-env
/tmp/barline-build-env/bin/python -m pip install build twine
/tmp/barline-build-env/bin/python -m build --outdir /tmp/barline-dist
/tmp/barline-build-env/bin/python -m twine check /tmp/barline-dist/*
```

CI builds a wheel and sdist, installs each into a fresh virtual environment, and runs `tests/smoke_distribution.py` from outside the checkout. That exercises installed commands through checks, stale evidence, recovery and optional native hook evaluation. This complements the full core suite; it does not establish native client trust behavior.

## One-time account setup

The maintainer needs a PyPI account with verified email and two-factor authentication. Do not paste credentials or recovery codes into agent chats.

For a new setup, create a pending publisher on [PyPI's publishing page](https://pypi.org/manage/account/publishing/) using:

| Field | Value |
| --- | --- |
| PyPI project name | `barline` |
| GitHub owner | `SSBrouhard` |
| GitHub repository | `barline` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

The PyPI username is the account creating the pending publisher; GitHub owner is a separate field. Read back the registered publisher before considering account setup complete. Pending publishers do not reserve project names. If PyPI rejects the name, stop and choose an available distribution name before publishing.

Configure the GitHub `pypi` environment to restrict deployments to tags matching `v*` and require the owner as reviewer (allowing self-review for this single-maintainer repository). Verify both settings before a release. GitHub plans can limit environment protections on private repositories. The workflow refuses private-repository publication and requires a manual tagged run with matching version confirmation. No API token secret is needed.

Official references: [create a project through Trusted Publishing](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/), [publish through GitHub Actions](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

## Publish a release

Public upload requires owner authorization. The workflow cannot publish while the repository is private, does not run on push/release events, and requires a manual run at a version tag with a matching version confirmation.

After approval to make the repository public and publish, verify the publisher and environment above, land a green release commit, and create a tag matching the metadata version (for example `v0.3.0`). Run **Publish to PyPI** at that tag and enter `0.3.0` as the confirmation. The workflow reruns the suite, builds distributions and checks metadata before the isolated publishing job requests an OIDC token.

Inspect the workflow result and PyPI project owner, version, files, hashes and attestations. Then install the exact version into a fresh environment using public instructions. A green build without a completed upload is not a release; a successful upload without installed verification is not a verified first-use result. PyPI versions are immutable, so repair a bad release with a new version, not an overwrite.
