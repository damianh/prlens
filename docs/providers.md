# AI Providers

prlens supports Anthropic, OpenAI, and GitHub Copilot. All providers use the same `BaseReviewer` prompt, retry, and review-comment contract.

---

## Anthropic Claude (default)

| Property | Value |
|---|---|
| Model | `claude-sonnet-4-20250514` |
| Temperature | `0.3` |
| Max output tokens | `4096` |
| Required env var | `ANTHROPIC_API_KEY` |
| Install extra | `pip install 'prlens[anthropic]'` |

### Setup

```bash
pip install 'prlens[anthropic]'
export ANTHROPIC_API_KEY=sk-ant-...
```

### Config

```yaml
model: anthropic
```

### Getting an API Key

Create a key at [console.anthropic.com/keys](https://console.anthropic.com/keys). The `claude-sonnet-4-20250514` model is available on all paid plans.

---

## OpenAI GPT-4o

| Property | Value |
|---|---|
| Model | `gpt-4o` |
| Temperature | `0.2` |
| Max output tokens | `4096` |
| Required env var | `OPENAI_API_KEY` |
| Install extra | `pip install 'prlens[openai]'` |

### Setup

```bash
pip install 'prlens[openai]'
export OPENAI_API_KEY=sk-...
```

### Config

```yaml
model: openai
```

### Getting an API Key

Create a key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys). GPT-4o access requires a funded account.

---

## GitHub Copilot for Actions

The Copilot provider uses the official `github-copilot-sdk` and the workflow's short-lived `GITHUB_TOKEN`. It is intended for GitHub Actions and requires Python 3.11 or later.

| Property | Value |
|---|---|
| Model | Copilot runtime default, or `copilot_model` |
| Request timeout | `120` seconds per attempt by default |
| Required env var | Explicit `GITHUB_TOKEN` |
| Install extra | `pip install 'packages/core[copilot]' packages/store packages/cli` from this fork |
| Workflow permission | `copilot-requests: write` |

This provider is currently implemented in the `damianh/prlens` fork and is not part of upstream PyPI 0.1.10. Clone this fork and install the local packages, or use the fork's composite action pinned to a full commit SHA.

```yaml
model: copilot
# copilot_model: gpt-5
# copilot_timeout: 120
```

Copilot sessions run in SDK empty mode with no available tools, repository instructions, skills, plugins, file hooks, host Git operations, extensions, or session store. PRLens supplies the diff and file context as prompt data. This prevents model-controlled filesystem or command access, but it is not an OS sandbox: the managed runtime still uses its own temporary state and contacts GitHub Copilot.

Copilot fails closed. Missing permissions, authentication/policy failures, exhausted retries, empty output, and invalid review JSON abort the run rather than being interpreted as a clean review.

### Diagnosing a failed request

Copilot failures include the phase (`runtime_start`, `create_session`, or
`send_and_wait`), error source, HTTP status when available, recognized SDK
error type/code and remediation action. These diagnostics survive retries and appear in
the final CLI error. Session errors are observed directly: the SDK's
`send_and_wait()` exception otherwise discards their structured metadata.

Authentication and other non-transient HTTP 4xx failures stop immediately;
429 throttling, 5xx, connection failures and unclassified failures retain bounded
retries. Explicit quota/billing and context-limit errors stop rather than retrying
an unchanged request.
A 403 means access was denied, not that any particular organization policy is
disabled. Compare the failing run's token permissions, authentication path and
model access before changing them. Remediation labels are diagnostic only:
PRLens never enables tools or relaxes isolation in response to one.

Diagnostics deliberately omit raw SDK messages, traces, URLs, request payloads
and review output. Recognized failure wording is converted to fixed,
application-authored messages; unknown text is not printed, since redacting
credentials alone cannot remove arbitrary private source snippets. Python SDK
logs are suppressed only within PRLens Copilot calls, including cleanup, and
safe provider diagnostics are emitted instead. Do not upload raw runtime logs
or enable traceback dumps to diagnose private reviews.

The diagnostics identify failure evidence, not a guaranteed remedy. If a failure
only occurs in Actions, publish and pin the diagnostic action revision, then start
a new workflow run using that pin. Rerunning an old run still uses its original
workflow revision. Keep the configured model and authentication restrictions
unchanged until the diagnostics support a specific change.

### Organization policy and billing

For organization-owned repositories, enable **Allow use of Copilot CLI billed to the organization** in the organization's Copilot policy. The workflow needs:

```yaml
permissions:
  contents: read
  pull-requests: write
  copilot-requests: write
```

Organization-owned repository usage is billed to the organization and does not use an individual user's budget. Model availability and billing remain subject to the organization's Copilot policies.

### Pull request safety

Use a normal `pull_request` workflow, skip pull requests from forks, and check out the trusted base SHA for configuration and guidelines. Do not switch to `pull_request_target` and execute untrusted pull-request code to obtain a privileged token. GitHub recommends guarded [Agentic Workflows](https://github.com/github/gh-aw) for general agentic automation; PRLens's integration is deliberately narrower and tool-free.

See [GitHub Action](github-action.md) for the complete workflow.

---

## Switching Providers

Override the config file for a single run:

```bash
prlens review --repo owner/repo --pr 42 --model copilot
```

Or change the default in `.prlens.yml`:

```yaml
model: copilot
```

---

## Shared provider flow

All providers share identical behaviour via the `BaseReviewer` class:

1. **System prompt** — injects the guidelines, sets the reviewer persona, and defines severity levels.
2. **User prompt** — includes the PR description, diff, full file content, and codebase context signals.
3. **API call with retry** — up to 3 attempts with exponential backoff (2s, 4s) on transient errors.
4. **JSON parsing** — strips any outer markdown fence (` ```json ``` `) from the response, then parses the JSON array. Backticks inside comment strings are preserved.

### Output Format

Providers are instructed to return a JSON array:

```json
[
  {
    "line": 42,
    "severity": "major",
    "comment": "Missing null check before dereferencing `user`.\n\n```python\nif user is None:\n    raise ValueError('user required')\n```"
  }
]
```

Comments may contain GitHub-flavored markdown — inline backticks for identifiers and triple-backtick fences with a language tag for code suggestions.

### Severity Levels

| Level | When to Use | GitHub Review Event |
|---|---|---|
| `critical` | Security vulnerability, data loss risk, crash | `REQUEST_CHANGES` |
| `major` | Logic bug, missing error handling, significant perf issue | `REQUEST_CHANGES` |
| `minor` | Code smell, unclear naming, missing type hint | `COMMENT` |
| `nitpick` | Style preference, minor formatting | `COMMENT` |

---

## Installing providers

```bash
pip install 'prlens[all]'
```

On Python 3.11+, `all` includes the Copilot SDK. On Python 3.9 and 3.10 it installs the existing Anthropic and OpenAI providers only. Selecting Copilot on an older interpreter produces an explicit compatibility error.
