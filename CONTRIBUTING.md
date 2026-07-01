# Contributing

## Running the tests

```bash
pip install -r requirements-test.txt
python -m pytest
```

The protocol tests load `custom_components/coolledx/device.py` and `ux_protocol.py`
standalone (no Home Assistant import needed).

## Continuous integration

Every push and pull request runs `.github/workflows/validate.yml`:

- **test** — the pytest suite
- **hassfest** — Home Assistant manifest/integration validation
- **hacs** — HACS repository validation

The workflow uses the `pull_request` trigger (not `pull_request_target`), so pull
requests from forks run with a read-only token and no access to repository secrets.

### Requiring approval for fork pull requests

Approval of fork workflows is a repository setting (it cannot live in a committed file).
To require your manual approval before **any** outside contributor's workflow runs:

> **Settings → Actions → General → Fork pull request workflows from outside
> collaborators → "Require approval for all outside collaborators"**

Pending PRs then show "workflows awaiting approval" until you click **Approve and run**.

## Cutting a release

Releases are automated by `.github/workflows/release.yml`:

1. Go to the **Actions** tab → **Release** → **Run workflow**.
2. Enter the version, e.g. `0.2.0` (no `v` prefix).

The workflow validates the version, runs the tests, bumps
`custom_components/coolledx/manifest.json`, commits and pushes the bump, creates the
`v<version>` tag, and publishes a GitHub Release with generated notes. HACS installs
the integration directly from the tagged repository source.

> **Note:** if the default branch is later protected to forbid direct pushes, the
> release job's push of the version-bump commit will fail — either allow
> `github-actions[bot]` to bypass the protection, or change the release flow to open a
> pull request instead.
