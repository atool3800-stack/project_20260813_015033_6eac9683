#!/usr/bin/env python3
"""
sync_metrics_to_readme.py
=========================
Daily GitHub efficiency dashboard generator for the
``project_20260813_015033_6eac9683`` open-source project.

The script:

1. Uses the GitHub REST API (v3) to paginate through **all** issues, pull
   requests, comments and commits created/updated in the last 24 hours.
2. Handles API **rate limiting**, **pagination cursors** and **failure
   retries** (with exponential backoff) so it can run unattended every day
   on large, active repositories (1,000 - 10,000 events/day).
3. Cleans & aggregates the raw events and computes daily efficiency metrics:
   * issue average first response time (time to first comment)
   * pull request average merge time
   * created / closed counts (issues & PRs)
   * active discussion count
   * label distribution
   * Top 10 active threads
4. Generates a Markdown "Daily Efficiency Report" and replaces the block
   between ``<!-- EFFICIENCY_REPORT_START -->`` and
   ``<!-- EFFICIENCY_REPORT_END -->`` in ``README.md`` without touching the
   rest of the document.

Usage
-----
    python scripts/sync_metrics_to_readme.py \\
        --owner atool3800-stack \\
        --repo project_20260813_015033_6eac9683 \\
        --token "$GITHUB_TOKEN" \\
        --out README.md

Environment variables
---------------------
``GITHUB_TOKEN`` is used when ``--token`` is not supplied.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE = "https://api.github.com"
DEFAULT_PER_PAGE = 100
MAX_RETRIES = 5
BASE_BACKOFF = 2.0          # seconds, exponential backoff base
RATE_LIMIT_SAFETY_MARGIN = 5  # keep at least N requests in the tank

REPORT_START = "<!-- EFFICIENCY_REPORT_START -->"
REPORT_END = "<!-- EFFICIENCY_REPORT_END -->"


# ---------------------------------------------------------------------------
# GitHub API client with rate-limit awareness, pagination & retries
# ---------------------------------------------------------------------------

class GitHubClient:
    """Minimal GitHub REST API client.

    Features:
    * follows ``Link`` headers for pagination
    * honours rate-limit headers (``X-RateLimit-Remaining`` / ``X-RateLimit-Reset``)
    * retries failed / throttled requests with exponential backoff + jitter
    """

    def __init__(self, token: str, owner: str, repo: str):
        self.token = token
        self.owner = owner
        self.repo = repo
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "project_20260813_015033_6eac9683-daily-sync",
        }

    # -- low level ----------------------------------------------------------
    def _request(self, url: str, method: str = "GET", body: Optional[dict] = None) -> Tuple[int, Any, Dict[str, str]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(url, data=data, method=method, headers=self._headers)
        if body is not None:
            req.add_header("Content-Type", "application/json")

        attempt = 0
        while True:
            attempt += 1
            try:
                with urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    status = resp.status
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    payload = json.loads(raw.decode("utf-8")) if raw else None
                    self._throttle_if_needed(headers)
                    return status, payload, headers
            except HTTPError as e:
                status = e.code
                headers = {k.lower(): v for k, v in e.headers.items()}
                payload = self._safe_json(e.read())
                # 4xx client errors that will never succeed -> abort
                if 400 <= status < 500 and status not in (403, 429):
                    raise RuntimeError(f"GitHub API {status} for {url}: {payload}")
                # Rate limited (403 with rate-limit header or 429)
                if status in (403, 429):
                    self._wait_for_rate_limit(headers)
                    continue
                # 5xx server errors -> retry with backoff
                if attempt <= MAX_RETRIES:
                    self._backoff(attempt, status)
                    continue
                raise RuntimeError(f"GitHub API {status} for {url} after {MAX_RETRIES} retries")
            except URLError as e:
                if attempt <= MAX_RETRIES:
                    self._backoff(attempt, reason=repr(e))
                    continue
                raise RuntimeError(f"Network error for {url}: {e}") from e

    @staticmethod
    def _safe_json(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"raw": raw.decode("utf-8", errors="replace")[:500]}

    # -- rate limiting ------------------------------------------------------
    @staticmethod
    def _throttle_if_needed(headers: Dict[str, str]) -> None:
        """Sleep proactively if we're close to the API rate limit."""
        try:
            remaining = int(headers.get("x-ratelimit-remaining", "99999"))
            reset = int(headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            return
        if remaining <= RATE_LIMIT_SAFETY_MARGIN:
            wait = max(reset - time.time(), 1) + 1
            print(f"[rate-limit] only {remaining} requests left; sleeping {wait:.0f}s "
                  f"until {datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()}", flush=True)
            time.sleep(wait)

    def _wait_for_rate_limit(self, headers: Dict[str, str]) -> None:
        try:
            reset = int(headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            reset = 0
        wait = max(reset - time.time(), 1) + 1
        print(f"[rate-limit] hit limit; sleeping {wait:.0f}s", flush=True)
        time.sleep(wait)

    def _backoff(self, attempt: int, status: Optional[int] = None, reason: str = "") -> None:
        delay = BASE_BACKOFF * (2 ** (attempt - 1)) + (attempt * 0.25)
        detail = f"HTTP {status}" if status else reason
        print(f"[retry] {detail} - attempt {attempt}/{MAX_RETRIES}, sleeping {delay:.1f}s", flush=True)
        time.sleep(delay)

    # -- pagination ----------------------------------------------------------
    def get_all(self, path: str, params: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
        """Iterate over every page of a paginated GitHub endpoint."""
        params = dict(params or {})
        params.setdefault("per_page", DEFAULT_PER_PAGE)
        url = f"{API_BASE}/repos/{self.owner}/{self.repo}/{path.lstrip('/')}?{urlencode(params)}"
        page = 0
        while url:
            page += 1
            _, payload, headers = self._request(url)
            if payload is None:
                break
            if isinstance(payload, list):
                if not payload:
                    break
                yield from payload
            else:
                # Some endpoints return a dict (e.g. search); not used here.
                yield payload
                break
            url = self._next_url(headers)

    @staticmethod
    def _next_url(headers: Dict[str, str]) -> Optional[str]:
        link = headers.get("link", "")
        for part in link.split(","):
            seg = part.strip()
            if 'rel="next"' in seg:
                m = re.search(r"<([^>]+)>", seg)
                if m:
                    return m.group(1)
        return None

    # -- single object helpers ----------------------------------------------
    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        qs = urlencode(params or {})
        url = f"{API_BASE}/repos/{self.owner}/{self.repo}/{path.lstrip('/')}"
        if qs:
            url += f"?{qs}"
        _, payload, _ = self._request(url)
        return payload

    def post(self, path: str, body: dict) -> Any:
        url = f"{API_BASE}/repos/{self.owner}/{self.repo}/{path.lstrip('/')}"
        _, payload, _ = self._request(url, method="POST", body=body)
        return payload


# ---------------------------------------------------------------------------
# Data model & cleaning
# ---------------------------------------------------------------------------

def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def hours_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    if not start or not end:
        return None
    delta = end - start
    return max(delta.total_seconds() / 3600.0, 0.0)


class EventSet:
    """Normalised, cleaned collection of GitHub events for the window."""

    def __init__(self):
        self.issues: List[Dict[str, Any]] = []
        self.pulls: List[Dict[str, Any]] = []
        self.issue_comments: List[Dict[str, Any]] = []
        self.pr_comments: List[Dict[str, Any]] = []
        self.commits: List[Dict[str, Any]] = []

    @property
    def total_events(self) -> int:
        return (
            len(self.issues)
            + len(self.pulls)
            + len(self.issue_comments)
            + len(self.pr_comments)
            + len(self.commits)
        )


def fetch_events(client: GitHubClient, since: datetime) -> EventSet:
    """Fetch all issues/PRs/comments/commits updated since ``since`` (UTC)."""
    events = EventSet()
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Issues (includes PRs; we separate them later by the ``pull_request`` key)
    print("[fetch] issues ...", flush=True)
    for item in client.get_all("issues", {"state": "all", "since": since_iso}):
        if "pull_request" in item:
            events.pulls.append(item)
        else:
            events.issues.append(item)

    # Issue comments
    print("[fetch] issue comments ...", flush=True)
    for item in client.get_all("issues/comments", {"since": since_iso}):
        events.issue_comments.append(item)

    # Pull request review comments
    print("[fetch] PR review comments ...", flush=True)
    for item in client.get_all("pulls/comments", {"since": since_iso}):
        events.pr_comments.append(item)

    # Commits
    print("[fetch] commits ...", flush=True)
    for item in client.get_all("commits", {"since": since_iso}):
        events.commits.append(item)

    return events


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def _issue_author(item: Dict[str, Any]) -> str:
    user = item.get("user") or {}
    return user.get("login") or "unknown"


def compute_metrics(events: EventSet) -> Dict[str, Any]:
    """Aggregate raw events into daily efficiency metrics."""
    now = datetime.now(timezone.utc)

    # ---- 1. Issue average first response time -----------------------------
    # Map issue -> list of (comment_time, commenter)
    comments_by_issue: Dict[int, List[Tuple[datetime, str]]] = defaultdict(list)
    for c in events.issue_comments:
        issue_url = c.get("issue_url") or ""
        m = re.search(r"/issues/(\d+)$", issue_url)
        if not m:
            continue
        num = int(m.group(1))
        author = (c.get("user") or {}).get("login") or "unknown"
        created = parse_iso(c.get("created_at"))
        if created:
            comments_by_issue[num].append((created, author))

    first_response_seconds: List[float] = []
    for issue in events.issues:
        num = issue.get("number")
        created = parse_iso(issue.get("created_at"))
        if not created or num is None:
            continue
        replies = [c for c in comments_by_issue.get(num, []) if c[0] >= created]
        if replies:
            first = min(t for t, _ in replies)
            first_response_seconds.append((first - created).total_seconds())

    # ---- 2. PR average merge time ------------------------------------------
    merge_seconds: List[float] = []
    for pr in events.pulls:
        created = parse_iso(pr.get("created_at"))
        merged = parse_iso(pr.get("merged_at"))
        if created and merged:
            merge_seconds.append((merged - created).total_seconds())

    # ---- 3. Created / closed counts ----------------------------------------
    created_issues = sum(1 for i in events.issues if i.get("created_at"))
    closed_issues = sum(1 for i in events.issues if i.get("closed_at"))
    created_prs = sum(1 for p in events.pulls if p.get("created_at"))
    closed_prs = sum(1 for p in events.pulls if p.get("closed_at"))
    merged_prs = sum(1 for p in events.pulls if p.get("merged_at"))

    # ---- 4. Active discussions ---------------------------------------------
    # A thread is "active" if it has >= 2 non-author comments OR received a
    # comment in the window AND is still open.
    active_threads: List[Dict[str, Any]] = []
    for item in events.issues + events.pulls:
        num = item.get("number")
        if num is None:
            continue
        comments = comments_by_issue.get(num, [])
        if len(comments) >= 2 and item.get("state") == "open":
            active_threads.append(item)

    # ---- 5. Label distribution ---------------------------------------------
    label_counter: Counter[str] = Counter()
    for item in events.issues + events.pulls:
        for lbl in item.get("labels") or []:
            name = lbl.get("name")
            if name:
                label_counter[name] += 1

    # ---- 6. Top 10 active threads -------------------------------------------
    thread_activity: List[Tuple[int, int, str, str, int, datetime]] = []
    all_threads = events.issues + events.pulls
    for item in all_threads:
        num = item.get("number")
        if num is None:
            continue
        created = parse_iso(item.get("created_at"))
        n_comments = len(comments_by_issue.get(num, []))
        n_reviews = sum(1 for pc in events.pr_comments if _pr_number(pc) == num)
        last_activity = max(
            [t for t, _ in comments_by_issue.get(num, [])] or [created or now]
        )
        thread_activity.append(
            (n_comments + n_reviews, num, item.get("title") or "", item.get("html_url") or "",
             _issue_author(item), last_activity)
        )
    thread_activity.sort(key=lambda x: (x[0], x[5]), reverse=True)
    top10 = thread_activity[:10]

    def _avg(seconds: List[float]) -> Optional[float]:
        return (sum(seconds) / len(seconds)) if seconds else None

    def _fmt_duration(seconds: Optional[float]) -> str:
        if seconds is None:
            return "—"
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{seconds / 60:.1f}m"
        if seconds < 86400:
            return f"{seconds / 3600:.1f}h"
        return f"{seconds / 86400:.1f}d"

    return {
        "generated_at": now,
        "total_events": events.total_events,
        "n_issues": len(events.issues),
        "n_pulls": len(events.pulls),
        "n_issue_comments": len(events.issue_comments),
        "n_pr_comments": len(events.pr_comments),
        "n_commits": len(events.commits),
        "issue_first_response_avg": _fmt_duration(_avg(first_response_seconds)),
        "issue_first_response_raw_avg": _avg(first_response_seconds),
        "pr_merge_time_avg": _fmt_duration(_avg(merge_seconds)),
        "pr_merge_time_raw_avg": _avg(merge_seconds),
        "created_issues": created_issues,
        "closed_issues": closed_issues,
        "created_prs": created_prs,
        "closed_prs": closed_prs,
        "merged_prs": merged_prs,
        "active_discussions": len(active_threads),
        "label_distribution": label_counter.most_common(),
        "top_threads": top10,
    }


def _pr_number(comment: Dict[str, Any]) -> Optional[int]:
    url = comment.get("pull_request_url") or comment.get("issue_url") or ""
    m = re.search(r"/(?:pulls|issues)/(\d+)$", url)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------

def render_report(metrics: Dict[str, Any], since: datetime, until: datetime) -> str:
    date_label = until.strftime("%Y-%m-%d")
    lines: List[str] = []
    lines.append(f"### 📊 Daily Efficiency Report — {date_label}")
    lines.append("")
    lines.append(f"_Statistics window: **{since.strftime('%Y-%m-%d %H:%M')} UTC** → "
                 f"**{until.strftime('%Y-%m-%d %H:%M')} UTC** · "
                 f"generated {metrics['generated_at'].strftime('%Y-%m-%d %H:%M UTC')}_")
    lines.append("")
    lines.append(f"**Total events processed: {metrics['total_events']:,}**")
    lines.append("")
    lines.append("#### Key metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Issues | {metrics['n_issues']} |")
    lines.append(f"| Pull requests | {metrics['n_pulls']} |")
    lines.append(f"| Issue comments | {metrics['n_issue_comments']} |")
    lines.append(f"| PR review comments | {metrics['n_pr_comments']} |")
    lines.append(f"| Commits | {metrics['n_commits']} |")
    lines.append(f"| Issue avg first response time | {metrics['issue_first_response_avg']} |")
    lines.append(f"| PR avg merge time | {metrics['pr_merge_time_avg']} |")
    lines.append(f"| Issues created / closed | {metrics['created_issues']} / {metrics['closed_issues']} |")
    lines.append(f"| PRs created / closed | {metrics['created_prs']} / {metrics['closed_prs']} |")
    lines.append(f"| PRs merged | {metrics['merged_prs']} |")
    lines.append(f"| Active discussions | {metrics['active_discussions']} |")
    lines.append("")

    # Label distribution
    lines.append("#### 🏷️ Label distribution")
    lines.append("")
    if metrics["label_distribution"]:
        lines.append("| Label | Count |")
        lines.append("|---|---|")
        for name, count in metrics["label_distribution"]:
            lines.append(f"| {name} | {count} |")
    else:
        lines.append("_No labels found in the window._")
    lines.append("")

    # Top 10 active threads
    lines.append("#### 🔥 Top 10 active threads")
    lines.append("")
    if metrics["top_threads"]:
        lines.append("| # | Thread | Author | Comments | Last activity |")
        lines.append("|---|---|---|---|---|")
        for rank, (activity, num, title, url, author, last) in enumerate(metrics["top_threads"], 1):
            title_clean = (title or "").replace("|", "\\|").strip()
            link = f"[#{num} {title_clean}]({url})" if url else f"#{num} {title_clean}"
            last_label = last.strftime("%m-%d %H:%M") if last else "—"
            lines.append(f"| {rank} | {link} | {author} | {activity} | {last_label} |")
    else:
        lines.append("_No active threads in the window._")
    lines.append("")

    return "\n".join(lines)


def update_readme(readme_path: Path, report: str) -> bool:
    """Replace the block between the markers; return True if README changed."""
    if not readme_path.exists():
        raise FileNotFoundError(f"README not found: {readme_path}")
    content = readme_path.read_text(encoding="utf-8")

    if REPORT_START not in content or REPORT_END not in content:
        raise RuntimeError(
            f"README markers not found. Expected '{REPORT_START}' and '{REPORT_END}' in {readme_path}"
        )

    pattern = re.compile(
        re.escape(REPORT_START) + r".*?" + re.escape(REPORT_END), re.DOTALL
    )
    new_block = f"{REPORT_START}\n{report}\n{REPORT_END}"
    new_content, n = pattern.subn(new_block, content, count=1)
    if n == 0:
        raise RuntimeError("Failed to replace efficiency report block")
    if new_content == content:
        return False
    readme_path.write_text(new_content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate & sync daily GitHub efficiency report to README.md")
    parser.add_argument("--owner", default="atool3800-stack", help="GitHub owner")
    parser.add_argument("--repo", default="project_20260813_015033_6eac9683", help="GitHub repository")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""), help="GitHub personal access token")
    parser.add_argument("--out", default="README.md", help="Path to README.md")
    parser.add_argument("--hours", type=int, default=24, help="Look-back window in hours (default 24)")
    parser.add_argument("--since", default=None, help="Explicit ISO start time (overrides --hours)")
    parser.add_argument("--no-write", action="store_true", help="Print report to stdout instead of writing README")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.token:
        print("ERROR: no GitHub token provided (use --token or GITHUB_TOKEN env)", file=sys.stderr)
        return 2

    until = datetime.now(timezone.utc)
    if args.since:
        since = parse_iso(args.since)
        if since is None:
            print(f"ERROR: could not parse --since {args.since}", file=sys.stderr)
            return 2
    else:
        since = until - timedelta(hours=args.hours)

    client = GitHubClient(token=args.token, owner=args.owner, repo=args.repo)
    events = fetch_events(client, since)
    metrics = compute_metrics(events)
    report = render_report(metrics, since, until)

    if args.no_write:
        print(report)
        return 0

    readme = Path(args.out)
    changed = update_readme(readme, report)
    print(f"[ok] total events: {metrics['total_events']:,}")
    print(f"[ok] README {'updated' if changed else 'unchanged'}: {readme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
