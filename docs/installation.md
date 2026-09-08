# Installation

## Requirements

- Python 3.9 or later
- A GitHub account with access to the repository you want to review
- An Anthropic/OpenAI API key, or eligible organization-billed GitHub Copilot usage

The Copilot provider requires Python 3.11 or later.

---

## Install from PyPI

Install with your preferred AI provider:

```bash
pip install 'prlens[anthropic]'   # Claude (default)
pip install 'prlens[openai]'      # GPT-4o
pip install 'prlens[all]'         # all providers available on this Python version
```

Installing `prlens` automatically pulls in `prlens-core` and `prlens-store`.

The Copilot provider is currently available from this fork rather than upstream PyPI 0.1.10:

```bash
git clone https://github.com/damianh/prlens.git
cd prlens
pip install 'packages/core[copilot]' packages/store packages/cli
python -m copilot download-runtime
```

---

## GitHub Token

prlens needs a GitHub token to fetch PR diffs and post review comments.

**Option A — Environment variable (CI / explicit):**
```bash
export GITHUB_TOKEN=ghp_...
```

**Option B — GitHub CLI (zero-friction local use):**

If you already use `gh`, prlens will reuse your existing session automatically:
```bash
gh auth login   # run once
# no GITHUB_TOKEN needed after this
```

The token must have `pull_requests: write` permission on the target repository. Copilot does not use the local `gh auth token` fallback: it requires an explicit `GITHUB_TOKEN`, and Actions must also grant `copilot-requests: write`.

---

## AI Provider Keys

Set the API key for your chosen provider:

```bash
# Anthropic Claude
export ANTHROPIC_API_KEY=sk-ant-...

# OpenAI GPT-4o
export OPENAI_API_KEY=sk-...
```

---

## Verify Installation

```bash
prlens --version
prlens --help
```

---

## Next Steps

- [Quick Start](quickstart.md) — run your first review
- [Configuration](configuration.md) — set up `.prlens.yml`
- [GitHub Action](github-action.md) — automate reviews on every PR
