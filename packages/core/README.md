# prlens-core

Core review engine for [prlens](https://github.com/prlens/prlens) — AI-powered GitHub PR code reviewer for teams.

## What's in this package

- **AI providers** — `BaseReviewer` + implementations for Anthropic, OpenAI, and GitHub Copilot
- **Codebase context** — injects repository file tree, co-change history, and paired test files into every review
- **GitHub API client** — fetches PR diffs, posts inline review comments, all pinned to the PR's head SHA
- **Config loader** — reads `.prlens.yml` and merges environment variables

## Installation

```bash
pip install 'prlens-core[anthropic]'   # Claude
pip install 'prlens-core[openai]'      # GPT-4o
pip install 'prlens-core[copilot]'     # GitHub Copilot, Python 3.11+
pip install 'prlens-core[all]'         # all providers supported by this Python
```

This package is a library dependency of [`prlens`](https://pypi.org/project/prlens/). Install `prlens` directly unless you are embedding the review engine in your own tool.

## Usage

```python
from prlens_core.reviewer import run_review
from prlens_core.config import load_config

config = load_config(".prlens.yml")
config["github_token"] = "ghp_..."

summary = run_review(repo="owner/repo", pr_number=42, config=config)
print(summary.total_comments, summary.event)
```

## Links

- [Documentation & CLI](https://github.com/prlens/prlens)
- [Changelog](https://github.com/prlens/prlens/releases)
- [Issues](https://github.com/prlens/prlens/issues)
