"""
CI Failure Investigator — polls all repos owned by GH_OWNER for recent workflow
failures, diagnoses each via Claude, and opens a fix PR in the target repo.
"""

import ast
import base64
import io
import json
import logging
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GH_PAT = os.environ["GH_PAT"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GH_OWNER = os.environ["GH_OWNER"]

# Watcher repo where processed_runs.json lives (this repo)
WATCHER_REPO = os.environ.get("GITHUB_REPOSITORY", f"{GH_OWNER}/ci-investigator")

LOOKBACK_MINUTES = 90  # investigate failures from the last 90 minutes
PROCESSED_FILE = Path(__file__).resolve().parent.parent / "processed_runs.json"
MAX_SOURCE_BYTES = 80_000  # skip files larger than this
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".git", "dist", "build"}
SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".yml", ".yaml", ".sh", ".toml", ".cfg"}

GH_HEADERS = {
    "Authorization": f"Bearer {GH_PAT}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def load_processed() -> set[str]:
    if not PROCESSED_FILE.exists():
        return set()
    data = json.loads(PROCESSED_FILE.read_text())
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    kept = [
        e for e in data.get("processed", [])
        if datetime.fromisoformat(e["ts"]) > cutoff
    ]
    return {e["key"] for e in kept}


def save_processed(processed: set[str], new_keys: list[str]) -> None:
    existing: list[dict] = []
    if PROCESSED_FILE.exists():
        data = json.loads(PROCESSED_FILE.read_text())
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        existing = [
            e for e in data.get("processed", [])
            if datetime.fromisoformat(e["ts"]) > cutoff
        ]
    now = datetime.now(timezone.utc).isoformat()
    for key in new_keys:
        existing.append({"key": key, "ts": now})
    PROCESSED_FILE.write_text(json.dumps({"processed": existing}, indent=2))


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def gh_get(url: str, params: Optional[dict] = None, stream: bool = False):
    resp = requests.get(url, headers=GH_HEADERS, params=params, stream=stream, timeout=30)
    resp.raise_for_status()
    return resp


def list_repos() -> list[dict]:
    repos = []
    page = 1
    while True:
        data = gh_get(
            "https://api.github.com/user/repos",
            params={"per_page": 100, "page": page, "type": "owner"},
        ).json()
        if not data:
            break
        repos.extend(data)
        page += 1
    return repos


def list_failed_runs(owner: str, repo: str, since: datetime) -> list[dict]:
    try:
        data = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs",
            params={"status": "failure", "per_page": 10},
        ).json()
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return []
        raise
    runs = data.get("workflow_runs", [])
    return [
        r for r in runs
        if datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")) > since
    ]


def fetch_run_logs(owner: str, repo: str, run_id: int) -> str:
    try:
        resp = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            stream=True,
        )
    except requests.HTTPError as e:
        return f"[Could not fetch logs: {e}]"

    content = b"".join(resp.iter_content(chunk_size=8192))
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return content.decode("utf-8", errors="replace")[:8000]

    parts = []
    for name in zf.namelist():
        with zf.open(name) as f:
            parts.append(f"=== {name} ===\n{f.read().decode('utf-8', errors='replace')}")
    full = "\n".join(parts)
    # Keep last 8000 chars — most relevant error context is at the end
    return full[-8000:] if len(full) > 8000 else full


def fetch_repo_sources(owner: str, repo: str, ref: str = "HEAD") -> dict[str, str]:
    try:
        tree = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}",
            params={"recursive": "1"},
        ).json()
    except requests.HTTPError:
        return {}

    sources: dict[str, str] = {}
    for item in tree.get("tree", []):
        if item["type"] != "blob":
            continue
        path = item["path"]
        ext = Path(path).suffix
        if ext not in SOURCE_EXTENSIONS:
            continue
        parts = Path(path).parts
        if any(d in SKIP_DIRS for d in parts):
            continue
        if item.get("size", 0) > MAX_SOURCE_BYTES:
            continue
        try:
            blob = gh_get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            ).json()
            raw = base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
            sources[path] = raw
        except Exception as e:
            logger.warning("Could not fetch %s: %s", path, e)
    return sources


# ---------------------------------------------------------------------------
# Claude investigation
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert CI/CD debugging assistant. Your job is to analyze GitHub Actions \
workflow failure logs and the corresponding source code, identify the root cause, \
and propose a minimal targeted fix.

Output ONLY a single valid JSON object — no markdown, no explanation outside the JSON. \
Use this exact schema:

{
  "root_cause": "one-sentence diagnosis of what failed and why",
  "fix_description": "what you changed and why it fixes the issue",
  "confidence": "high | medium | low",
  "patches": [
    {
      "file": "relative/path/to/file.py",
      "original": "exact string to find and replace (must match exactly)",
      "replacement": "corrected string"
    }
  ],
  "no_code_fix": false,
  "notes": "optional context for the reviewer"
}

Rules:
- patches must use exact string matches — never approximate
- do not change function signatures, env var names, or import paths unless they are the root cause
- if the failure is caused by a missing secret, external service outage, or is not fixable in code, \
  set patches to [] and no_code_fix to true
- keep patches minimal — only change what is broken
"""


def call_claude(logs: str, sources: dict[str, str]) -> dict:
    source_block = "\n\n".join(
        f"--- {path} ---\n{content}" for path, content in sources.items()
    )
    user_message = f"""FAILURE LOGS:
{logs}

SOURCE FILES:
{source_block}

Analyze the failure and produce the JSON fix."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = msg.content[0].text.strip()
    # Strip accidental markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_patch(file_path: str, new_content: str) -> tuple[bool, str]:
    ext = Path(file_path).suffix
    if ext == ".py":
        try:
            ast.parse(new_content)
        except SyntaxError as e:
            return False, str(e)
    elif ext in {".yml", ".yaml"}:
        try:
            import yaml  # available via requests dep chain; safe to try
            yaml.safe_load(new_content)
        except Exception as e:
            return False, str(e)
    return True, "ok"


# ---------------------------------------------------------------------------
# GitHub PR creation
# ---------------------------------------------------------------------------


def ensure_labels(owner: str, repo: str) -> None:
    for label, color, desc in [
        ("automated", "0075ca", "Opened by CI investigator"),
        ("ci-failure", "e4e669", "Auto-investigated workflow failure"),
    ]:
        requests.post(
            f"https://api.github.com/repos/{owner}/{repo}/labels",
            headers=GH_HEADERS,
            json={"name": label, "color": color, "description": desc},
            timeout=10,
        )  # ignore errors — label may already exist


def get_default_branch_sha(owner: str, repo: str, branch: str) -> str:
    ref = gh_get(
        f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}"
    ).json()
    return ref["object"]["sha"]


def create_branch(owner: str, repo: str, branch: str, sha: str) -> None:
    resp = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/git/refs",
        headers=GH_HEADERS,
        json={"ref": f"refs/heads/{branch}", "sha": sha},
        timeout=10,
    )
    if resp.status_code not in (201, 422):  # 422 = branch already exists
        resp.raise_for_status()


def apply_patch_via_api(
    owner: str, repo: str, branch: str, file_path: str, new_content: str, message: str
) -> None:
    # Get current file SHA (required for update)
    try:
        existing = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
            params={"ref": branch},
        ).json()
        file_sha = existing["sha"]
    except requests.HTTPError:
        file_sha = None  # new file

    payload: dict = {
        "message": message,
        "content": base64.b64encode(new_content.encode()).decode(),
        "branch": branch,
    }
    if file_sha:
        payload["sha"] = file_sha

    resp = requests.put(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
        headers=GH_HEADERS,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def open_pr(
    owner: str,
    repo: str,
    branch: str,
    default_branch: str,
    run_id: int,
    result: dict,
    validation_notes: list[str],
) -> str:
    patches = result.get("patches", [])
    files_changed = "\n".join(f"- `{p['file']}`" for p in patches) or "_No files changed_"
    validation_summary = "\n".join(validation_notes) or "No patches to validate."
    fix_text = result.get("fix_description") or "No code fix identified — diagnosis only."
    run_url = f"https://github.com/{owner}/{repo}/actions/runs/{run_id}"

    body = f"""## Root Cause
{result.get("root_cause", "Unknown")}

## Fix Applied
{fix_text}

## Confidence
{result.get("confidence", "unknown")}

## Files Changed
{files_changed}

## Notes
{result.get("notes") or "_None_"}

## Failed Workflow Run
{run_url}

## Validation
{validation_summary}

---
_Opened automatically by [ci-investigator](https://github.com/{WATCHER_REPO})_"""

    resp = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers=GH_HEADERS,
        json={
            "title": f"fix(auto): investigate workflow failure — run #{run_id}",
            "body": body,
            "head": branch,
            "base": default_branch,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["html_url"]


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def process_run(owner: str, repo: str, run: dict) -> None:
    run_id = run["id"]
    run_name = run.get("name", "unknown")
    logger.info("Investigating %s/%s run #%s (%s)", owner, repo, run_id, run_name)

    logs = fetch_run_logs(owner, repo, run_id)
    sources = fetch_repo_sources(owner, repo, run.get("head_sha", "HEAD"))

    try:
        result = call_claude(logs, sources)
    except Exception as e:
        logger.error("Claude API call failed for %s/%s run %s: %s", owner, repo, run_id, e)
        return

    patches = result.get("patches", [])
    logger.info(
        "Claude diagnosis (confidence=%s): %s — %d patches",
        result.get("confidence"),
        result.get("root_cause"),
        len(patches),
    )

    # Determine default branch
    repo_meta = gh_get(f"https://api.github.com/repos/{owner}/{repo}").json()
    default_branch = repo_meta.get("default_branch", "main")
    head_sha = get_default_branch_sha(owner, repo, default_branch)

    fix_branch = f"fix/auto-{run_id}"
    create_branch(owner, repo, fix_branch, head_sha)

    validation_notes: list[str] = []
    applied_patches = []

    for patch in patches:
        file_path = patch["file"]
        original = patch["original"]
        replacement = patch["replacement"]

        # Fetch current file content
        try:
            blob = gh_get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}",
                params={"ref": fix_branch},
            ).json()
            current = base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
        except Exception as e:
            validation_notes.append(f"- `{file_path}`: could not fetch — {e}")
            continue

        if original not in current:
            validation_notes.append(
                f"- `{file_path}`: original string not found — skipped"
            )
            continue

        new_content = current.replace(original, replacement, 1)
        valid, reason = validate_patch(file_path, new_content)
        if not valid:
            validation_notes.append(
                f"- `{file_path}`: syntax validation FAILED — {reason} (patch skipped)"
            )
            continue

        try:
            apply_patch_via_api(
                owner, repo, fix_branch, file_path, new_content,
                f"fix(auto): patch {file_path} for run #{run_id}"
            )
            validation_notes.append(f"- `{file_path}`: patch applied, syntax OK")
            applied_patches.append(patch)
        except Exception as e:
            validation_notes.append(f"- `{file_path}`: failed to apply — {e}")

    result["patches"] = applied_patches

    ensure_labels(owner, repo)
    try:
        pr_url = open_pr(
            owner, repo, fix_branch, default_branch, run_id, result, validation_notes
        )
        logger.info("PR opened: %s", pr_url)
    except Exception as e:
        logger.error("Failed to open PR for %s/%s run %s: %s", owner, repo, run_id, e)


def main() -> None:
    processed = load_processed()
    since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)

    repos = list_repos()
    logger.info("Scanning %d repos for failures since %s", len(repos), since.isoformat())

    new_keys: list[str] = []

    for repo_data in repos:
        owner = repo_data["owner"]["login"]
        repo = repo_data["name"]

        failed_runs = list_failed_runs(owner, repo, since)
        for run in failed_runs:
            key = f"{owner}/{repo}:{run['id']}"
            if key in processed:
                continue
            process_run(owner, repo, run)
            new_keys.append(key)

    if new_keys:
        save_processed(processed, new_keys)
        logger.info("Processed %d new failures", len(new_keys))

        # Commit updated processed_runs.json back to watcher repo
        watcher_owner, watcher_repo = WATCHER_REPO.split("/")
        try:
            apply_patch_via_api(
                watcher_owner,
                watcher_repo,
                "main",
                "processed_runs.json",
                PROCESSED_FILE.read_text(),
                f"chore: mark {len(new_keys)} run(s) as processed",
            )
        except Exception as e:
            logger.warning("Could not commit processed_runs.json: %s", e)
    else:
        logger.info("No new failures found")


if __name__ == "__main__":
    missing = [k for k in ("GH_PAT", "ANTHROPIC_API_KEY", "GH_OWNER") if not os.environ.get(k)]
    if missing:
        logger.error("Missing required env vars: %s", missing)
        sys.exit(1)
    main()
