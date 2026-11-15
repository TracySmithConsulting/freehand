"""Reading a GitHub project: open issues, open pull requests, and recent
test runs. Read only, and for public projects it needs no account or key
(GitHub allows 60 of these a hour without one). Used by the Developer
team member template (core/team.py), and by FreeHand when asked about a
GitHub project."""
from __future__ import annotations

import re

API = "https://api.github.com"


def _repo(value: str) -> str:
    value = (value or "").strip()
    m = re.search(r"github\.com/([\w.\-]+/[\w.\-]+?)(?:\.git)?(?:/|$)", value)
    repo = m.group(1) if m else value.strip("/")
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo):
        raise ValueError("Name the project as owner/name, for example TracySmithConsulting/freehand.")
    return repo


async def github_activity(repo: str) -> dict:
    """Open issues and pull requests (newest first) and the latest test runs."""
    import aiohttp
    try:
        repo = _repo(repo)
    except ValueError as e:
        return {"error": str(e)}
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "FreeHand"}
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(f"{API}/repos/{repo}/issues",
                                   params={"state": "open", "per_page": "15", "sort": "updated"}) as r:
                if r.status == 404:
                    return {"error": f"GitHub has no public project called {repo}."}
                if r.status == 403:
                    return {"error": "GitHub says FreeHand has asked too often. Try again in an hour."}
                items = await r.json() if r.status == 200 else []
            async with session.get(f"{API}/repos/{repo}/actions/runs", params={"per_page": "5"}) as r:
                runs = (await r.json()).get("workflow_runs", []) if r.status == 200 else []
    except Exception as e:
        return {"error": f"GitHub couldn't be reached: {str(e)[:100]}"}
    issues, pulls = [], []
    for it in items if isinstance(items, list) else []:
        entry = {"number": it.get("number"), "title": it.get("title"), "by": (it.get("user") or {}).get("login"),
                 "updated": it.get("updated_at"), "url": it.get("html_url")}
        (pulls if it.get("pull_request") else issues).append(entry)
    tests = [{"name": run.get("name"), "branch": run.get("head_branch"), "status": run.get("status"),
              "result": run.get("conclusion"), "when": run.get("created_at")} for run in runs]
    return {"repo": repo, "open_issues": issues, "open_pull_requests": pulls, "recent_test_runs": tests}
