# GitHub Action

prlens provides a composite GitHub Action that runs automatically on every pull request. No server, no polling — just drop in a workflow file.

---

## Quick Setup

### Option A — `prlens init` (recommended)

Run `prlens init` locally and answer yes when asked to generate the workflow file. It creates `.github/workflows/prlens.yml` with the correct permissions, secrets, and trigger configuration.

### Option B — Manual

Create `.github/workflows/prlens.yml` in any repository:

```yaml
name: PR Lens Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write

    steps:
      - uses: actions/checkout@v4

      - uses: prlens/prlens/.github/actions/review@main
        with:
          model: anthropic
          github-token: ${{ secrets.GITHUB_TOKEN }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

Then add `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) to your repository secrets:
**Settings → Secrets and variables → Actions → New repository secret**

---

## GitHub Copilot without a provider secret

The Copilot provider is currently supplied by the `damianh/prlens` fork. Pin the action to the full commit SHA that contains this feature:

```yaml
name: PR Lens Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      copilot-requests: write

    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.base.sha }}
          persist-credentials: false

      - uses: damianh/prlens/.github/actions/review@<full-commit-sha>
        with:
          model: copilot
          github-token: ${{ github.token }}
```

No Anthropic key, OpenAI key, PAT, or GitHub App installation is needed. The action installs all three PRLens packages from its own pinned fork revision, then provisions the matching managed Copilot runtime. The `version` input applies only to PyPI-backed Anthropic/OpenAI installations.

Before using this workflow, enable **Allow use of Copilot CLI billed to the organization** in the organization's Copilot policy. Usage for organization-owned repositories is billed to that organization and is subject to its model and billing policies; it does not use an individual user's budget.

The fork guard and base-SHA checkout are deliberate. Ordinary fork PR workflows generally cannot receive the required write permissions, and pull-request changes must not be allowed to replace the reviewer configuration or installed provider. Do not use `pull_request_target` to execute untrusted PR code as a workaround.

Copilot runs without model tools or repository access. PRLens fetches PR-head content through GitHub APIs and sends it as untrusted prompt data. This is not an OS sandbox: the managed runtime still contacts Copilot and uses temporary runtime/state files. For broader agentic automation, GitHub recommends guarded [Agentic Workflows](https://github.com/github/gh-aw).

---

## Action Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `model` | No | `anthropic` | AI provider: `anthropic`, `openai`, or `copilot` |
| `github-token` | **Yes** | — | GitHub token with `pull-requests: write` |
| `anthropic-api-key` | No | `""` | Required when `model: anthropic` |
| `openai-api-key` | No | `""` | Required when `model: openai` |
| `guidelines` | No | `""` | Path to a Markdown guidelines file (relative to repo root) |
| `config-path` | No | `.prlens.yml` | Path to `.prlens.yml` (relative to repo root) |
| `full-review` | No | `false` | Set to `true` to re-review all files on every run |

The caller grants workflow permissions; a composite action cannot add `copilot-requests: write` itself.

---

## How the PR Number is Determined

The action uses GitHub's built-in event context — no configuration needed:

- `github.repository` → `owner/repo`
- `github.event.pull_request.number` → the PR that triggered the workflow

Both are injected automatically by the Actions runner when the workflow triggers on a `pull_request` event.

---

## GITHUB_TOKEN vs Personal Access Token

The built-in `GITHUB_TOKEN` (provided automatically by GitHub Actions) is sufficient to post review comments. With `copilot-requests: write`, it also authenticates the Copilot provider. It acts as the `github-actions[bot]` user, not as any real developer.

```yaml
github-token: ${{ secrets.GITHUB_TOKEN }}   # default — use this
```

To have comments appear as a specific user (a dedicated bot account, for example), create a Personal Access Token (PAT) for that account with `pull_requests: write`, store it as a secret (e.g. `PRLENS_GITHUB_TOKEN`), and use it instead:

```yaml
github-token: ${{ secrets.PRLENS_GITHUB_TOKEN }}   # custom user
```

> **Note:** If you use the Gist store, the built-in `GITHUB_TOKEN` does **not** have Gist permissions. You must use a PAT with `gist` scope. See [History Stores — Gist](stores.md#gist-store).

---

## Using Guidelines from a Central Repository

If your team maintains guidelines in a shared repository rather than each individual repo, check out that repository first.

### Private guidelines repo

```yaml
steps:
  - uses: actions/checkout@v4

  - uses: actions/checkout@v4
    with:
      repository: your-org/engineering-standards
      path: .guidelines
      token: ${{ secrets.GUIDELINES_REPO_TOKEN }}  # PAT with contents: read
      sparse-checkout: |
        guidelines/code-review.md

  - uses: prlens/prlens/.github/actions/review@main
    with:
      model: anthropic
      github-token: ${{ secrets.GITHUB_TOKEN }}
      anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
      guidelines: .guidelines/guidelines/code-review.md
```

### Public guidelines repo

```yaml
  - uses: actions/checkout@v4
    with:
      repository: your-org/engineering-standards
      path: .guidelines
      # no token needed for public repos

  - uses: prlens/prlens/.github/actions/review@main
    with:
      guidelines: .guidelines/guidelines/code-review.md
      # ... other inputs
```

---

## Using OpenAI

```yaml
- uses: prlens/prlens/.github/actions/review@main
  with:
    model: openai
    github-token: ${{ secrets.GITHUB_TOKEN }}
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```

## Using Copilot

Use the complete pinned example above. `prlens init` can generate it after you provide the full commit SHA for the Copilot-enabled fork action. The token-only Copilot wizard does not offer Gist storage because the workflow token has no Gist scope.

---

## Full Example with All Options

```yaml
name: PR Lens Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write

    steps:
      - uses: actions/checkout@v4

      - uses: prlens/prlens/.github/actions/review@main
        with:
          model: anthropic
          github-token: ${{ secrets.GITHUB_TOKEN }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          guidelines: docs/review-guidelines.md
          config-path: .prlens.yml
          full-review: 'false'
```

---

## Secrets Checklist

| Secret | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | If using Claude | Add in repo Settings → Secrets → Actions |
| `OPENAI_API_KEY` | If using GPT-4o | Add in repo Settings → Secrets → Actions |
| `GITHUB_TOKEN` | **Auto-provided** | Do not add manually |
| Provider API key | Not needed for Copilot | Uses `${{ github.token }}` with `copilot-requests: write` |
| `PRLENS_GITHUB_TOKEN` | Only if using a custom bot user | PAT with `pull_requests: write` |
| `GUIDELINES_REPO_TOKEN` | Only if guidelines repo is private | PAT with `contents: read` on guidelines repo |
