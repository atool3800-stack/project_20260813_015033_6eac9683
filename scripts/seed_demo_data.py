#!/usr/bin/env python3
"""
seed_demo_data.py
=================
Helper used to seed the repository with realistic GitHub activity so the
daily efficiency pipeline has data to process (issues, PRs, comments and
commits). In a production deployment the repo naturally accumulates this
traffic; this script is provided so the dashboard can be exercised on a
fresh repository and for local demos.

Handles GitHub primary & secondary rate limits and paces write requests.

Requires: GITHUB_TOKEN env var (or --token).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API_BASE = "https://api.github.com"
WRITE_PACE = 1.0  # seconds between write calls to avoid secondary rate limits

LABELS = [
    {"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
    {"name": "enhancement", "color": "a2eeef", "description": "New feature or request"},
    {"name": "documentation", "color": "0075ca", "description": "Improvements or additions to documentation"},
    {"name": "question", "color": "d876e3", "description": "Further information is requested"},
    {"name": "help wanted", "color": "008672", "description": "Extra attention is needed"},
    {"name": "good first issue", "color": "7057ff", "description": "Good for newcomers"},
    {"name": "priority: high", "color": "b60205", "description": "High priority"},
    {"name": "priority: medium", "color": "fbca04", "description": "Medium priority"},
    {"name": "priority: low", "color": "0e8a16", "description": "Low priority"},
    {"name": "tech-debt", "color": "5319e7", "description": "Technical debt"},
]

ISSUE_TITLES = [
    "Fix pagination bug on /api/events endpoint",
    "Add retry logic for flaky CI tests",
    "Improve error messages for invalid API keys",
    "Document rate-limit handling in the README",
    "Refactor metrics aggregation into a reusable module",
    "Support timezone-aware timestamps in reports",
    "Add caching layer for expensive queries",
    "Fix flaky e2e test for dashboard rendering",
    "Upgrade dependencies to latest minor versions",
    "Add unit tests for the label distribution logic",
    "Reduce cold-start latency for the metrics worker",
    "Improve accessibility of the report tables",
    "Add CSV export for daily metrics",
    "Handle empty dataset gracefully in dashboard",
    "Add monitoring alert for API 429 responses",
    "Migrate config to environment-variable based setup",
    "Fix memory leak in the event stream processor",
    "Add pagination cursor validation",
    "Document the daily sync workflow in CONTRIBUTING",
    "Add a health check endpoint for the scheduler",
    "Optimize the Top-10 thread query with an index",
    "Add badge showing last-sync time to README",
    "Fix race condition in concurrent comment writers",
    "Standardize ISO-8601 formatting across the codebase",
    "Add integration test for first-response-time metric",
    "Include PR merge time in the weekly digest",
    "Handle GitHub API secondary rate limits",
    "Add a dry-run flag to the sync script",
    "Normalize label casing when aggregating",
    "Add structured logging to the fetcher",
]

PR_TITLES = [
    "feat: add daily efficiency dashboard",
    "fix: correct first-response-time calculation",
    "docs: document rate-limit handling",
    "chore: update dependencies",
    "refactor: extract metrics module",
    "fix: handle empty dataset in renderer",
    "feat: add CSV export",
    "test: add unit tests for aggregator",
    "perf: optimize thread ranking query",
    "fix: timezone bug in report timestamps",
]

COMMENT_TEMPLATES = [
    "Thanks for the detailed write-up — I'll take a look today.",
    "Could you share a minimal reproduction? That would help a lot.",
    "This looks good to me. +1",
    "I think we should scope this to the next milestone.",
    "Agreed, let's track this in a follow-up issue.",
    "Great catch! Let me verify locally and get back to you.",
    "We hit the same problem last week; the fix is to bump the client timeout.",
    "Can we add a test case for this edge scenario?",
    "Makes sense. I'll assign myself and start on it.",
    "The new approach looks cleaner. Nice work!",
    "One nit: could we use ISO-8601 consistently here?",
    "This resolves the pagination issue we saw in production.",
    "Let's add this to the changelog for the next release.",
    "I reproduced it on v2.3.1 — the issue is the cursor handling.",
    "Thanks for the quick turnaround on this PR!",
    "Reviewed and approved. Merging now.",
]


class Api:
    def __init__(self, token: str):
        self.token = token
        self.last_write = 0.0

    def _pace(self):
        """Pace write requests to avoid GitHub secondary rate limits."""
        elapsed = time.time() - self.last_write
        if elapsed < WRITE_PACE:
            time.sleep(WRITE_PACE - elapsed)

    def request(self, method: str, url: str, body=None, write=False):
        if write:
            self._pace()
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "project-seed",
            "Content-Type": "application/json",
        })
        for attempt in range(8):
            try:
                with urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode()
                    if write:
                        self.last_write = time.time()
                    return resp.status, (json.loads(raw) if raw else {})
            except HTTPError as e:
                if write:
                    self.last_write = time.time()
                if e.code in (403, 429):
                    retry_after = e.headers.get("Retry-After")
                    reset = e.headers.get("X-RateLimit-Reset")
                    wait = None
                    if retry_after:
                        wait = float(retry_after)
                    elif reset:
                        wait = max(float(reset) - time.time(), 1) + 1
                    if wait and wait <= 3600:
                        print(f"  [rate-limit] sleeping {wait:.0f}s", flush=True)
                        time.sleep(wait)
                        continue
                    if wait:
                        print(f"  [rate-limit] huge wait {wait:.0f}s, aborting", flush=True)
                        return e.code, {"message": "rate limited"}
                if e.code >= 500 and attempt < 7:
                    time.sleep(2 ** attempt)
                    continue
                return e.code, {"message": e.read().decode()[:400]}
        return 500, {"message": "failed"}


def gh_url(owner, repo, path, **params):
    qs = urlencode(params) if params else ""
    url = f"{API_BASE}/repos/{owner}/{repo}/{path}"
    return url + (f"?{qs}" if qs else "")


def run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[git] {' '.join(cmd)} failed: {r.stderr[:300]}", flush=True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="atool3800-stack")
    ap.add_argument("--repo", default="project_20260813_015033_6eac9683")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--n-issues", type=int, default=150)
    ap.add_argument("--n-prs", type=int, default=40)
    ap.add_argument("--n-comments", type=int, default=500)
    ap.add_argument("--n-commits", type=int, default=350)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if not args.token:
        sys.exit("No GITHUB_TOKEN provided")
    rng = random.Random(args.seed)
    api = Api(args.token)

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    git = lambda cmd: run(cmd, repo_dir)

    # ---------- labels ----------
    print("[seed] creating labels", flush=True)
    for lbl in LABELS:
        api.request("POST", gh_url(args.owner, args.repo, "labels"), lbl, write=True)
    print(f"[seed] labels ready ({len(LABELS)})", flush=True)

    # ---------- issues ----------
    issue_nums = []
    for i in range(args.n_issues):
        title = rng.choice(ISSUE_TITLES)
        n_labels = rng.randint(0, 3)
        labels = [l["name"] for l in rng.sample(LABELS, n_labels)] if n_labels else []
        state = "closed" if rng.random() < 0.35 else "open"
        body = f"Auto-seeded issue {i+1}: {title}\n\nEnvironment: prod\nSeverity: {rng.choice(['low','medium','high'])}"
        status, issue = api.request("POST", gh_url(args.owner, args.repo, "issues"), {
            "title": f"{title} (#{i+1})", "body": body, "labels": labels,
        }, write=True)
        if status in (200, 201) and "number" in issue:
            issue_nums.append(issue["number"])
            if state == "closed":
                api.request("PATCH", gh_url(args.owner, args.repo, f"issues/{issue['number']}"),
                            {"state": "closed"}, write=True)
        if (i + 1) % 25 == 0:
            print(f"[seed] issues {i+1}/{args.n_issues}", flush=True)

    # ---------- comments ----------
    for i in range(args.n_comments):
        if not issue_nums:
            break
        num = rng.choice(issue_nums)
        api.request("POST", gh_url(args.owner, args.repo, f"issues/{num}/comments"),
                    {"body": rng.choice(COMMENT_TEMPLATES)}, write=True)
        if (i + 1) % 50 == 0:
            print(f"[seed] comments {i+1}/{args.n_comments}", flush=True)

    # ---------- PRs (via git branches + API) ----------
    git(["checkout", "main"])
    for i in range(args.n_prs):
        branch = f"feat/seed-pr-{i+1}"
        git(["checkout", "-b", branch, "main"])
        with open(os.path.join(repo_dir, "data", f"seed_pr_{i+1}.txt"), "w") as f:
            f.write(f"seed content for PR {i+1}\n")
        git(["add", "data/seed_pr_{}.txt".format(i + 1)])
        git(["commit", "-m", f"seed: changes for PR {i+1} [skip ci]"])
        git(["push", "-u", "origin", branch])
        title = rng.choice(PR_TITLES)
        status, pr = api.request("POST", gh_url(args.owner, args.repo, "pulls"), {
            "title": f"{title} (PR {i+1})",
            "head": branch,
            "base": "main",
            "body": f"Seeded pull request {i+1}.",
        }, write=True)
        if status in (200, 201) and "number" in pr:
            roll = rng.random()
            if roll < 0.30:
                api.request("PUT", gh_url(args.owner, args.repo, f"pulls/{pr['number']}/merge"),
                            {"merge_method": "squash"}, write=True)
                git(["checkout", "main"])
                git(["merge", branch])
                git(["push", "origin", "main"])
            elif roll < 0.55:
                api.request("PATCH", gh_url(args.owner, args.repo, f"pulls/{pr['number']}"),
                            {"state": "closed"}, write=True)
        git(["checkout", "main"])
        if (i + 1) % 10 == 0:
            print(f"[seed] PRs {i+1}/{args.n_prs}", flush=True)

    # ---------- commits on main ----------
    git(["checkout", "main"])
    for i in range(args.n_commits):
        with open(os.path.join(repo_dir, "data", f"commit_{i+1}.txt"), "w") as f:
            f.write(f"commit content {i+1}\n")
        git(["add", "data/commit_{}.txt".format(i + 1)])
        git(["commit", "-m", f"chore: sync commit {i+1} [skip ci]"])
    git(["push", "origin", "main"])

    print("[seed] done", flush=True)


if __name__ == "__main__":
    main()
