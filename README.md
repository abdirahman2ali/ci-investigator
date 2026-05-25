# ci-investigator

Watches all your GitHub repos for workflow failures. When one is detected, it fetches the logs, sends them to Claude for diagnosis, proposes a code fix, and opens a PR in the target repo for review.

Runs on a schedule every 30 minutes. No changes needed to the repos being watched.

## How it works

1. `watcher.yml` triggers every 30 minutes (or manually)
2. `scripts/investigate.py` calls the GitHub API to find recent failures across all repos you own
3. For each unprocessed failure, it fetches the logs and source files, sends them to Claude, and parses a structured diagnosis
4. If Claude produces patches, they are validated (syntax check) then committed to a `fix/auto-<run_id>` branch in the target repo
5. A PR is opened with the root cause, proposed fix, confidence level, and validation results
6. `processed_runs.json` is updated to prevent duplicate PRs

## Setup

### 1. Create the repo on GitHub

```sh
gh repo create abdirahman2ali/ci-investigator --public --source=. --push
```

### 2. Add secrets

In the `ci-investigator` repo settings, add:

| Secret | Description |
|--------|-------------|
| `GH_PAT` | Personal Access Token with `repo` and `workflow` scopes |
| `ANTHROPIC_API_KEY` | Anthropic API key |

To create the PAT: GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Select all repos, grant `Contents` (read/write), `Pull requests` (read/write), `Actions` (read).

### 3. Trigger manually to test

```sh
gh workflow run watcher.yml --repo abdirahman2ali/ci-investigator
```

## Environment variables

| Variable | Description | Example |
|----------|-------------|---------|
| `GH_PAT` | GitHub PAT with repo + workflow access | `github_pat_...` |
| `ANTHROPIC_API_KEY` | Anthropic API key | `sk-ant-...` |
| `GH_OWNER` | GitHub username (set automatically in Actions) | `abdirahman2ali` |

## Running locally

```sh
cp .env.example .env
# fill in .env values
pip install anthropic requests
GH_OWNER=abdirahman2ali python scripts/investigate.py
```
