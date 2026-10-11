"""
Jira REST API client — creates subtasks, transitions issues, creates bug reports.
Uses Basic Auth (email + API token).
"""
import time
import requests
from requests.auth import HTTPBasicAuth
import config

_auth = HTTPBasicAuth(config.JIRA_EMAIL, config.JIRA_API_TOKEN)
_headers = {"Accept": "application/json", "Content-Type": "application/json"}
_base = config.JIRA_BASE_URL.rstrip("/")


def _get(path: str) -> dict:
    r = requests.get(f"{_base}{path}", auth=_auth, headers=_headers)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = requests.post(f"{_base}{path}", auth=_auth, headers=_headers, json=body)
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} for url: {r.url}\nBody: {r.text}", response=r
        )
    return r.json() if r.text else {}


def _put(path: str, body: dict) -> dict:
    r = requests.put(f"{_base}{path}", auth=_auth, headers=_headers, json=body)
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} for url: {r.url}\nBody: {r.text}", response=r
        )
    return r.json() if r.text else {}


# ── Story / issue lookup ──────────────────────────────────────────────────────

def get_issue(issue_key: str) -> dict:
    """Return full Jira issue dict."""
    return _get(f"/rest/api/3/issue/{issue_key}")


def issue_exists(issue_key: str) -> bool:
    """Return True if the issue exists in Jira, False if it was deleted (404)."""
    try:
        _get(f"/rest/api/3/issue/{issue_key}?fields=key")
        return True
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return False
        raise


def get_issue_summary(issue_key: str) -> str:
    return get_issue(issue_key)["fields"]["summary"]


def get_issue_description(issue_key: str) -> str:
    """Return plain-text description extracted from Atlassian Document Format."""
    fields = get_issue(issue_key)["fields"]
    desc = fields.get("description")
    if not desc:
        return ""
    return adf_to_text(desc)


def adf_to_text(node) -> str:
    """Render an ADF document to plain text, walking EVERY nesting level.

    Lists, tables, panels and expands nest text 3-6 levels deep; a two-level
    walk silently dropped all of it (e.g. TAT-7095 kept 638 of 7215 chars), so
    the generator never saw most of the requirements. Structure is kept as
    markdown so table rows and list items stay distinct requirements.
    """
    if not isinstance(node, dict):
        return ""
    return _render_adf(node).strip()


def _render_adf(node: dict, depth: int = 0) -> str:
    t = node.get("type")
    kids = node.get("content", []) or []
    attrs = node.get("attrs", {}) or {}

    def inline(n):
        return "".join(_render_adf(c, depth) for c in n.get("content", []) or [])

    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t in ("mention", "emoji"):
        return attrs.get("text") or attrs.get("shortName", "")
    if t in ("inlineCard", "blockCard", "embedCard"):
        return attrs.get("url", "")
    if t == "status":
        return f"[{attrs.get('text', '')}]"
    if t == "date":
        return str(attrs.get("timestamp", ""))
    if t in ("paragraph",):
        return inline(node) + "\n"
    if t == "heading":
        return "#" * int(attrs.get("level", 2)) + " " + inline(node) + "\n"
    if t in ("bulletList", "orderedList"):
        out = []
        for i, item in enumerate(kids, 1):
            marker = f"{i}." if t == "orderedList" else "-"
            body = "".join(_render_adf(c, depth + 1) for c in item.get("content", []) or []).strip()
            body = body.replace("\n", "\n" + "  " * (depth + 1))
            out.append("  " * depth + f"{marker} {body}")
        return "\n".join(out) + "\n"
    if t == "table":
        rows = []
        for row in kids:
            cells = [
                " ".join("".join(_render_adf(c) for c in cell.get("content", []) or []).split())
                for cell in row.get("content", []) or []
            ]
            rows.append("| " + " | ".join(cells) + " |")
        return "\n".join(rows) + "\n"
    if t == "codeBlock":
        return "```\n" + inline(node) + "\n```\n"
    if t == "expand":
        return f"[{attrs.get('title', 'Details')}]\n" + "".join(_render_adf(c, depth) for c in kids)
    if t == "rule":
        return "---\n"
    # doc, panel, blockquote, taskList, decisionList, layout*, media* ...
    return "".join(_render_adf(c, depth) for c in kids)


def get_sprint_issues(sprint_id: int) -> list[dict]:
    """Return all issues in a Jira Software sprint."""
    data = _get(f"/rest/agile/1.0/sprint/{sprint_id}/issue?maxResults=200")
    return data.get("issues", [])


_sprint_cache: dict = {}  # board_name -> (sprint_name, fetched_at)
_SPRINT_CACHE_TTL = 300   # 5 minutes


def get_active_sprint_for_board(board_name: str) -> str | None:
    """Return the active sprint name for a named Jira board, cached for 5 min."""
    now = time.time()
    cached = _sprint_cache.get(board_name)
    if cached and now - cached[1] < _SPRINT_CACHE_TTL:
        return cached[0]

    boards = _get(f"/rest/agile/1.0/board?name={requests.utils.quote(board_name)}&type=scrum")
    values = boards.get("values", [])
    if not values:
        print(f"  WARNING: No board found with name '{board_name}'")
        return None

    board_id = values[0]["id"]
    sprints = _get(f"/rest/agile/1.0/board/{board_id}/sprint?state=active")
    active = sprints.get("values", [])
    if not active:
        print(f"  WARNING: No active sprint found for board '{board_name}'")
        return None

    sprint_name = active[0]["name"]
    _sprint_cache[board_name] = (sprint_name, now)
    print(f"  Active sprint for '{board_name}': {sprint_name}")
    return sprint_name


_multi_assignee_field: str | None = None


def _get_multi_assignee_field() -> str | None:
    """Discover the 'Multiple Assignee' custom field ID (cached).
    Prefers fields whose name contains 'assignee'; falls back to first array-user field."""
    global _multi_assignee_field
    if _multi_assignee_field is not None:
        return _multi_assignee_field
    try:
        fields = _get("/rest/api/3/field")
        if isinstance(fields, list):
            candidates = [
                f for f in fields
                if (f.get("schema", {}).get("type") == "array"
                    and f.get("schema", {}).get("items") == "user")
            ]
            # Prefer the field whose name explicitly mentions "assignee"
            for f in candidates:
                if "assignee" in f.get("name", "").lower():
                    _multi_assignee_field = f["id"]
                    print(f"  [Jira] Multi-assignee field: {f['name']} ({f['id']})")
                    return _multi_assignee_field
            # Fallback: first array-user field found
            if candidates:
                _multi_assignee_field = candidates[0]["id"]
                print(f"  [Jira] Multi-assignee fallback field: {candidates[0].get('name')} ({_multi_assignee_field})")
                return _multi_assignee_field
    except Exception:
        pass
    return None


def get_issue_comments(issue_key: str, max_results: int = 10) -> list[str]:
    """Return up to max_results comment bodies as plain text."""
    data = _get(f"/rest/api/3/issue/{issue_key}/comment?maxResults={max_results}&orderBy=-created")
    comments = []
    for c in data.get("comments", []):
        body = c.get("body", {})
        text = adf_to_text(body)
        if text:
            author = c.get("author", {}).get("displayName", "Unknown")
            comments.append(f"[{author}]: {text}")
    return comments


def get_linked_issues(issue_key: str) -> list[str]:
    """Return summaries of issues linked to this ticket (e.g. 'relates to', 'blocks')."""
    issue = get_issue(issue_key)
    links = issue.get("fields", {}).get("issuelinks", [])
    result = []
    for link in links:
        link_type = link.get("type", {}).get("name", "")
        for direction in ("inwardIssue", "outwardIssue"):
            linked = link.get(direction)
            if linked:
                key = linked.get("key", "")
                summary = linked.get("fields", {}).get("summary", "")
                result.append(f"{link_type} → {key}: {summary}")
    return result


def get_issues_by_jql(jql: str) -> list[dict]:
    """Return issues matching a JQL query."""
    extra = []
    field_id = _get_multi_assignee_field()
    if field_id:
        extra = [field_id]
    data = _post("/rest/api/3/search/jql", {
        "jql": jql,
        "maxResults": 100,
        "fields": ["key", "summary", "status", "priority", "assignee", "subtasks", "issuetype"] + extra,
    })
    return data.get("issues", [])


def get_subtask_assignees(subtask_to_story: dict[str, str]) -> dict[str, list[dict]]:
    """Fetch assignees for a set of subtask keys and map them back to their parent story.

    Args:
        subtask_to_story: {subtask_key: story_key} — built from the story's embedded
                          `subtasks` array, so we never rely on parent-field parsing.
    Returns:
        {story_key: [assignee_user_dict, ...]}
    """
    if not subtask_to_story:
        return {}
    keys_jql = ", ".join(subtask_to_story.keys())
    data = _post("/rest/api/3/search/jql", {
        "jql": f"issue in ({keys_jql})",
        "maxResults": 500,
        "fields": ["assignee"],
    })
    result: dict[str, list] = {}
    for issue in data.get("issues", []):
        st_key = issue.get("key", "")
        story_key = subtask_to_story.get(st_key, "")
        assignee = issue.get("fields", {}).get("assignee")
        if story_key and assignee:
            result.setdefault(story_key, []).append(assignee)
    return result


# ── Subtask creation ──────────────────────────────────────────────────────────

SUBTASK_NAMES = [
    "[Testing] Test case writing",
    "[Testing] Test case review",
    "[Testing] Test case execution - mocks",
    "[Testing] Test case execution - E2E",
]

# Subtask issue type ID for this Jira instance (confirmed from browser DevTools)
_SUBTASK_ISSUETYPE_ID = "10008"


def create_subtasks(parent_key: str) -> dict[str, str]:
    """
    Create the 3 QA subtasks under a parent story, skipping any that already exist.
    Uses numeric IDs for project, parent, and issuetype — matching the
    browser curl that works: POST /rest/api/2/issue with id-based fields.
    Returns {subtask_name: issue_key} for all subtasks (existing + newly created).
    """
    parent = get_issue(parent_key)
    project_id = str(parent["fields"]["project"]["id"])
    parent_id = str(parent["id"])

    existing_subtasks = {
        s["fields"]["summary"]: s["key"]
        for s in parent["fields"].get("subtasks", [])
    }

    result = {}
    for name in SUBTASK_NAMES:
        if name in existing_subtasks:
            print(f"  Skipping subtask (already exists): {existing_subtasks[name]} — {name}")
            result[name] = existing_subtasks[name]
            continue
        body = {
            "fields": {
                "project":   {"id": project_id},
                "parent":    {"id": parent_id},
                "summary":   name,
                "issuetype": {"id": _SUBTASK_ISSUETYPE_ID},
            }
        }
        resp = _post("/rest/api/2/issue", body)
        key = resp.get("key", "ERR")
        result[name] = key
        print(f"  Created subtask: {key} — {name}")
        _transition_to_todo(key)
    return result


# ── Transitions ──────────────────────────────────────────────────────────────

def get_transitions(issue_key: str) -> list[dict]:
    data = _get(f"/rest/api/3/issue/{issue_key}/transitions")
    return data.get("transitions", [])


def _transition_to_todo(issue_key: str) -> None:
    transitions = get_transitions(issue_key)
    todo_transition = next(
        (t for t in transitions if t["name"].lower() in ("to do", "todo", "open")),
        None
    )
    if not todo_transition:
        print(f"  WARNING: Could not find 'To Do' transition for {issue_key}")
        print(f"  Available: {[t['name'] for t in transitions]}")
        return
    _post(f"/rest/api/3/issue/{issue_key}/transitions", {"transition": {"id": todo_transition["id"]}})
    print(f"  Transitioned {issue_key} → To Do")


def transition_to_done(issue_key: str) -> bool:
    """Transition an issue to its 'Done' state."""
    transitions = get_transitions(issue_key)
    done_transition = next(
        (t for t in transitions if t["name"].lower() in ("done", "closed", "resolved")),
        None
    )
    if not done_transition:
        print(f"  WARNING: Could not find 'Done' transition for {issue_key}")
        print(f"  Available: {[t['name'] for t in transitions]}")
        return False
    _post(f"/rest/api/3/issue/{issue_key}/transitions", {"transition": {"id": done_transition["id"]}})
    print(f"  Transitioned {issue_key} → Done")
    return True


# ── Bug creation ──────────────────────────────────────────────────────────────

def create_bug(
    parent_key: str,
    title: str,
    description_adf: dict,
    screenshot_path: str | None = None,
) -> str:
    """
    Create a Bug issue linked to the parent story.
    Returns the new bug's issue key (e.g. TAT-999).
    """
    parent = get_issue(parent_key)
    project_key = parent["fields"]["project"]["key"]

    body = {
        "fields": {
            "project": {"key": project_key},
            "summary": title,
            "issuetype": {"name": "Bug"},
            "description": description_adf,
        }
    }
    resp = _post("/rest/api/3/issue", body)
    bug_key = resp.get("key", "ERR")

    # Link bug → story (blocks relationship)
    link_body = {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": bug_key},
        "outwardIssue": {"key": parent_key},
    }
    try:
        _post("/rest/api/3/issueLink", link_body)
        print(f"  Bug {bug_key} linked to {parent_key}")
    except Exception as e:
        print(f"  Warning: Could not link bug to story: {e}")

    # Attach screenshot if provided
    if screenshot_path:
        attach_file(bug_key, screenshot_path)

    return bug_key


def attach_file(issue_key: str, file_path: str) -> None:
    """Attach a file to a Jira issue."""
    import os
    if not os.path.exists(file_path):
        print(f"  WARNING: Screenshot not found: {file_path}")
        return
    with open(file_path, "rb") as f:
        requests.post(
            f"{_base}/rest/api/3/issue/{issue_key}/attachments",
            auth=_auth,
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (os.path.basename(file_path), f, "image/png")},
        ).raise_for_status()
    print(f"  Attached screenshot to {issue_key}")


def build_bug_description_adf(
    test_name: str,
    error_message: str,
    failed_step: str,
    story_key: str,
    aio_tc_key: str | None,
    environment: str = "preprod",
) -> dict:
    """Build an Atlassian Document Format body for a bug report."""
    lines = [
        ("heading", f"Bug Report — {test_name}"),
        ("paragraph", f"*Story:* {story_key}"),
        ("paragraph", f"*AIO Test Case:* {aio_tc_key or 'N/A'}"),
        ("paragraph", f"*Environment:* {environment}"),
        ("rule",),
        ("heading2", "Steps to Reproduce"),
        ("paragraph", failed_step),
        ("heading2", "Actual Result"),
        ("paragraph", error_message),
        ("heading2", "Expected Result"),
        ("paragraph", "Test should pass without errors."),
        ("heading2", "Severity"),
        ("paragraph", "High — automated regression detected this failure."),
    ]

    content = []
    for item in lines:
        if item[0] == "heading":
            content.append({"type": "heading", "attrs": {"level": 2},
                             "content": [{"type": "text", "text": item[1]}]})
        elif item[0] == "heading2":
            content.append({"type": "heading", "attrs": {"level": 3},
                             "content": [{"type": "text", "text": item[1]}]})
        elif item[0] == "paragraph":
            content.append({"type": "paragraph",
                             "content": [{"type": "text", "text": item[1]}]})
        elif item[0] == "rule":
            content.append({"type": "rule"})

    return {"type": "doc", "version": 1, "content": content}
