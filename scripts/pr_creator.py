"""
PR Creator — searches for open ci-failure issues created by ci-investigator,
parses proposed patches, applies them via GitHub Contents API, opens a PR,
and closes the issue with a reference comment.

Environment variables required:
    GH_PAT    — personal access token with repo + issues scope
    GH_OWNER  — GitHub account/org to search issues across
"""

import base64
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GH_PAT = os.environ["GH_PAT"]
GH_OWNER = os.environ["GH_OWNER"]
WATCHER_REPO = os.environ.get("GITHUB_REPOSITORY", f"{GH_OWNER}/ci-investigator")

DEDUP_MARKER = "<!-- pr-created -->"
BRANCH_PREFIX = "ci-fix"
SKIP_CONFIDENCE = {"low"}

GH_HEADERS = {
    "Authorization": f"Bearer {GH_PAT}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _handle_rate_limit(resp: requests.Response) -> None:
    reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
    sleep_for = max(reset - time.time(), 5)
    logger.warning("Rate limited — sleeping %.0fs", sleep_for)
    time.sleep(sleep_for)


def gh_get(url: str, params: Optional[dict] = None) -> requests.Response:
    resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=30)
    if resp.status_code in (429, 403) and "X-RateLimit-Remaining" in resp.headers:
        _handle_rate_limit(resp)
        resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp


def gh_post(url: str, payload: dict) -> requests.Response:
    resp = requests.post(url, headers=GH_HEADERS, json=payload, timeout=30)
    if resp.status_code in (429, 403) and "X-RateLimit-Remaining" in resp.headers:
        _handle_rate_limit(resp)
        resp = requests.post(url, headers=GH_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp


def gh_put(url: str, payload: dict) -> requests.Response:
    resp = requests.put(url, headers=GH_HEADERS, json=payload, timeout=30)
    if resp.status_code in (429, 403) and "X-RateLimit-Remaining" in resp.headers:
        _handle_rate_limit(resp)
        resp = requests.put(url, headers=GH_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp


def gh_patch(url: str, payload: dict) -> requests.Response:
    resp = requests.patch(url, headers=GH_HEADERS, json=payload, timeout=30)
    if resp.status_code in (429, 403) and "X-RateLimit-Remaining" in resp.headers:
        _handle_rate_limit(resp)
        resp = requests.patch(url, headers=GH_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Issue discovery
# ---------------------------------------------------------------------------


def search_ci_failure_issues() -> list[dict]:
    issues = []
    page = 1
    while True:
        data = gh_get(
            "https://api.github.com/search/issues",
            params={
                "q": f"is:open is:issue label:ci-failure label:automated user:{GH_OWNER}",
                "per_page": 100,
                "page": page,
            },
        ).json()
        items = data.get("items", [])
        if not items:
            break
        issues.extend(items)
        if len(issues) >= data.get("total_count", 0):
            break
        if page >= 10:
            logger.warning("Search result cap reached (1000 issues) — some may be skipped")
            break
        page += 1
    logger.info("Found %d open ci-failure issue(s)", len(issues))
    return issues


def parse_repo_from_issue(issue: dict) -> tuple[str, str]:
    # repository_url: "https://api.github.com/repos/owner/repo"
    parts = issue["repository_url"].rstrip("/").split("/")
    return parts[-2], parts[-1]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def is_already_processed(owner: str, repo: str, issue_number: int) -> bool:
    page = 1
    while True:
        comments = gh_get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
            params={"per_page": 100, "page": page},
        ).json()
        if not comments:
            return False
        for c in comments:
            if DEDUP_MARKER in c.get("body", ""):
                return True
        if len(comments) < 100:
            return False
        page += 1


# ---------------------------------------------------------------------------
# Issue body parsing
# ---------------------------------------------------------------------------

PATCH_BLOCK_RE = re.compile(
    r"### `(?P<file>[^`]+)`\s+"
    r"\*\*Original:\*\*\s*```[^\n]*\n(?P<original>.*?)```\s+"
    r"\*\*Replacement:\*\*\s*```[^\n]*\n(?P<replacement>.*?)```",
    re.DOTALL,
)

CONFIDENCE_RE = re.compile(r"^## Confidence\s*\n(?P<level>\S+)", re.MULTILINE)

RUN_URL_RE = re.compile(r"^## Failed Workflow Run\s*\n(https://\S+)", re.MULTILINE)


def parse_confidence(body: str) -> str:
    m = CONFIDENCE_RE.search(body)
    return m.group("level").lower() if m else "low"


def extract_run_url(body: str) -> str:
    m = RUN_URL_RE.search(body)
    return m.group(1) if m else ""


def has_patch_section(body: str) -> bool:
    if "## Proposed Patches" not in body:
        return False
    if "_Claude could not identify a code fix" in body:
        return False
    return True


def parse_patches(body: str) -> list[dict]:
    patches = []
    for m in PATCH_BLOCK_RE.finditer(body):
        patches.append({
            "file": m.group("file").strip(),
            # rstrip("\n") removes the template separator added by open_issue()
            "original": m.group("original").rstrip("\n"),
            "replacement": m.group("replacement").rstrip("\n"),
        })
    return patches


# ---------------------------------------------------------------------------
# Branch management
# ---------------------------------------------------------------------------


def get_default_branch(owner: str, repo: str) -> str:
    return gh_get(f"https://api.github.com/repos/{owner}/{repo}").json()["default_branch"]


def get_branch_sha(owner: str, repo: str, branch: str) -> str:
    data = gh_get(
        f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}"
    ).json()
    return data["object"]["sha"]


def _branch_exists(owner: str, repo: str, branch: str) -> bool:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
        headers=GH_HEADERS,
        timeout=30,
    )
    return resp.status_code == 200


def make_unique_branch_name(owner: str, repo: str, base_name: str) -> str:
    if not _branch_exists(owner, repo, base_name):
        return base_name
    for i in range(2, 21):
        candidate = f"{base_name}-{i}"
        if not _branch_exists(owner, repo, candidate):
            return candidate
    raise RuntimeError(
        f"No available branch name after 20 attempts (base: {base_name})"
    )


def create_branch(owner: str, repo: str, new_branch: str, sha: str) -> None:
    gh_post(
        f"https://api.github.com/repos/{owner}/{repo}/git/refs",
        {"ref": f"refs/heads/{new_branch}", "sha": sha},
    )


def build_branch_name(issue_number: int) -> str:
    return f"{BRANCH_PREFIX}/issue-{issue_number}"


# ---------------------------------------------------------------------------
# File patching via Contents API
# ---------------------------------------------------------------------------


def get_file(owner: str, repo: str, path: str, branch: str) -> tuple[str, str]:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        headers=GH_HEADERS,
        params={"ref": branch},
        timeout=30,
    )
    if resp.status_code == 404:
        raise FileNotFoundError(f"{path} not found in {owner}/{repo}@{branch}")
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return content, data["sha"]


def apply_replacement(content: str, original: str, replacement: str) -> tuple[str, int]:
    count = content.count(original)
    if count == 0:
        raise ValueError("Original string not found in file content")
    return content.replace(original, replacement), count


def put_file(
    owner: str,
    repo: str,
    path: str,
    branch: str,
    new_content: str,
    file_sha: str,
    commit_message: str,
) -> None:
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
    gh_put(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        {
            "message": commit_message,
            "content": encoded,
            "sha": file_sha,
            "branch": branch,
        },
    )


def apply_patches_to_branch(
    owner: str, repo: str, branch: str, patches: list[dict]
) -> tuple[list[str], list[str]]:
    """Apply all patches to branch. Returns (applied_summaries, warning_strings)."""
    applied: list[str] = []
    warnings: list[str] = []

    for patch in patches:
        file_path = patch["file"]
        try:
            content, file_sha = get_file(owner, repo, file_path, branch)
        except FileNotFoundError:
            warnings.append(f"Skipped `{file_path}`: file not found in repo")
            continue
        except Exception as e:
            warnings.append(f"Skipped `{file_path}`: unexpected error fetching file — {e}")
            continue

        try:
            new_content, count = apply_replacement(
                content, patch["original"], patch["replacement"]
            )
        except ValueError:
            warnings.append(f"Skipped `{file_path}`: original string not found in file")
            continue

        try:
            put_file(
                owner,
                repo,
                file_path,
                branch,
                new_content,
                file_sha,
                f"fix: apply ci-investigator patch to {file_path}",
            )
            applied.append(f"`{file_path}` ({count} replacement(s))")
            logger.info("Patched %s (%d replacement(s))", file_path, count)
        except Exception as e:
            warnings.append(f"Skipped `{file_path}`: failed to write — {e}")
            continue

        time.sleep(0.5)  # courtesy pause between writes

    return applied, warnings


# ---------------------------------------------------------------------------
# PR creation
# ---------------------------------------------------------------------------

_PR_BODY = """\
## Automated Fix

This PR was opened by [ci-investigator](https://github.com/{watcher_repo}) to \
address the CI failure diagnosed in issue #{issue_number}.

### Changes
{patch_summary}

### Patch Warnings
{warnings_block}

### Reference
- Issue: {issue_url}
- Failed run: {run_url}

---
_Review carefully before merging. Confidence: **{confidence}**._

{dedup_marker}
"""


def build_pr_body(
    issue_number: int,
    issue_url: str,
    run_url: str,
    applied: list[str],
    warnings: list[str],
    confidence: str,
) -> str:
    patch_summary = "\n".join(f"- {a}" for a in applied) if applied else "_No files patched._"
    warnings_block = "\n".join(f"- {w}" for w in warnings) if warnings else "_None_"
    return _PR_BODY.format(
        watcher_repo=WATCHER_REPO,
        issue_number=issue_number,
        patch_summary=patch_summary,
        warnings_block=warnings_block,
        issue_url=issue_url,
        run_url=run_url or "_unknown_",
        confidence=confidence,
        dedup_marker=DEDUP_MARKER,
    )


def open_pr(
    owner: str,
    repo: str,
    branch: str,
    default_branch: str,
    title: str,
    body: str,
) -> str:
    resp = gh_post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        {"title": title, "body": body, "head": branch, "base": default_branch},
    )
    return resp.json()["html_url"]


# ---------------------------------------------------------------------------
# Issue close-out
# ---------------------------------------------------------------------------


def comment_on_issue(owner: str, repo: str, issue_number: int, body: str) -> None:
    gh_post(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
        {"body": body},
    )


def close_issue(owner: str, repo: str, issue_number: int) -> None:
    gh_patch(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
        {"state": "closed", "state_reason": "completed"},
    )


# ---------------------------------------------------------------------------
# Per-issue orchestration
# ---------------------------------------------------------------------------


def process_issue(issue: dict) -> None:
    issue_number = issue["number"]
    issue_url = issue["html_url"]
    body = issue.get("body") or ""
    owner, repo = parse_repo_from_issue(issue)

    logger.info("Processing %s/%s#%d", owner, repo, issue_number)

    confidence = parse_confidence(body)
    if confidence in SKIP_CONFIDENCE:
        logger.info("Skipping #%d — confidence is %s", issue_number, confidence)
        return

    if not has_patch_section(body):
        logger.info("Skipping #%d — no parseable patch section (no_code_fix)", issue_number)
        return

    if is_already_processed(owner, repo, issue_number):
        logger.info("Skipping #%d — dedup marker found in comments", issue_number)
        return

    patches = parse_patches(body)
    if not patches:
        logger.warning(
            "Skipping #%d — patch section present but no blocks parsed", issue_number
        )
        return

    run_url = extract_run_url(body)
    default_branch = get_default_branch(owner, repo)
    head_sha = get_branch_sha(owner, repo, default_branch)

    branch = make_unique_branch_name(owner, repo, build_branch_name(issue_number))
    create_branch(owner, repo, branch, head_sha)
    logger.info("Created branch %s in %s/%s", branch, owner, repo)

    applied, warnings = apply_patches_to_branch(owner, repo, branch, patches)

    if not applied:
        logger.error(
            "No patches applied for #%d — all %d patch(es) failed; skipping PR",
            issue_number,
            len(patches),
        )
        return

    pr_body = build_pr_body(issue_number, issue_url, run_url, applied, warnings, confidence)
    pr_title = f"fix(ci): automated patch for issue #{issue_number}"

    try:
        pr_url = open_pr(owner, repo, branch, default_branch, pr_title, pr_body)
        logger.info("Opened PR: %s", pr_url)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            logger.warning("PR already exists for branch %s — posting dedup marker anyway", branch)
            pr_url = f"(existing PR for branch {branch})"
        else:
            raise

    comment_body = (
        f"PR opened: {pr_url}\n\n"
        f"Applied {len(applied)} patch(es). "
        f"Closing this issue — see the PR for review.\n\n"
        f"{DEDUP_MARKER}"
    )
    comment_on_issue(owner, repo, issue_number, comment_body)
    close_issue(owner, repo, issue_number)
    logger.info("Closed issue #%d", issue_number)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    issues = search_ci_failure_issues()
    for issue in issues:
        try:
            process_issue(issue)
        except Exception as e:
            logger.error("Failed to process issue %s: %s", issue.get("html_url"), e)


if __name__ == "__main__":
    missing = [k for k in ("GH_PAT", "GH_OWNER") if not os.environ.get(k)]
    if missing:
        logger.error("Missing required env vars: %s", missing)
        sys.exit(1)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
