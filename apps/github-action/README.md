# prlens GitHub Action

Run `prlens init` to automatically generate `.github/workflows/prlens.yml`
with the correct permissions, secrets, and trigger configuration.

The generated workflow:
- Triggers on `pull_request` events (opened, synchronize, reopened)
- Installs prlens and the chosen AI provider
- Runs `prlens review --yes` using the built-in `GITHUB_TOKEN` (no PAT needed)
- Posts inline review comments directly on the PR

The GitHub Copilot provider additionally requires Python 3.11+, the
`copilot-requests: write` workflow permission, organization-billed Copilot CLI
usage to be enabled, a trusted base-SHA checkout, and a full commit SHA pin for
the `damianh/prlens` action. The generated workflow skips fork pull requests.

## Manual Setup

If you prefer to set up the workflow manually, copy the template from
`prlens init` output or refer to the prlens documentation.
