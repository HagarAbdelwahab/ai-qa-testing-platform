"""
Story Review Agent — generates a QA-lens review of a Jira user story.
Fetches story details + comments, then calls Claude to produce structured review.
"""
import requests
from requests.auth import HTTPBasicAuth
import config


def _auth():
    return HTTPBasicAuth(config.JIRA_EMAIL, config.JIRA_API_TOKEN)


def _extract_text(node) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(_extract_text(c) for c in node.get("content", []))
    if isinstance(node, list):
        return "".join(_extract_text(c) for c in node)
    return ""


def _get_full_description(issue: dict) -> str:
    """Extract full plain-text description from ADF."""
    desc = issue["fields"].get("description")
    if not desc:
        return ""
    parts = []
    def walk(node):
        if isinstance(node, dict):
            ntype = node.get("type", "")
            text = node.get("text", "")
            if text:
                parts.append(text)
            for child in node.get("content", []):
                walk(child)
            if ntype in ("paragraph", "heading", "listItem", "rule"):
                parts.append("\n")
        elif isinstance(node, list):
            for n in node:
                walk(n)
    walk(desc)
    return " ".join(parts).strip()


def _get_comments(issue_key: str) -> list[dict]:
    r = requests.get(
        f"{config.JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
        "?orderBy=created&maxResults=50",
        auth=_auth(),
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    return r.json().get("comments", [])


def generate_story_review(issue_key: str) -> str:
    from jira_client import get_issue
    from test_generator import _call_ai

    print(f"  [1/3] Reading story: {issue_key}")
    issue = get_issue(issue_key)
    fields = issue["fields"]
    story_title = fields["summary"]
    status = fields["status"]["name"]
    issue_type = fields["issuetype"]["name"]
    assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
    priority = (fields.get("priority") or {}).get("name", "None")
    description = _get_full_description(issue)
    print(f"  Title  : {story_title}")
    print(f"  Status : {status}  |  Assignee: {assignee}")

    print(f"  [2/3] Fetching comments...")
    comments_raw = _get_comments(issue_key)
    comments_text = ""
    for c in comments_raw[:15]:
        author = c.get("author", {}).get("displayName", "?")
        created = c.get("created", "")[:10]
        text = _extract_text(c.get("body", {})).strip()[:600]
        if text:
            comments_text += f"\n[{created}] {author}:\n{text}\n"
    if not comments_text:
        comments_text = "(no comments)"
    print(f"  Found {len(comments_raw)} comment(s).")

    print(f"  [3/3] Generating review with AI...")

    prompt = f"""You are a senior QA engineer reviewing a Jira user story for testability and completeness.

Issue   : {issue_key}
Type    : {issue_type}
Status  : {status}
Priority: {priority}
Assignee: {assignee}
Title   : {story_title}

--- DESCRIPTION ---
{description or "(no description provided)"}

--- COMMENTS ---
{comments_text}

Produce a structured QA review in exactly this markdown format:

## User Story Review — {issue_key}

**Summary:** {story_title}
**Status:** {status}

---
### Comments Summary
[Summarise key decisions or open questions from comments. If no comments, write "No comments on this story."]

---
### Scores

| Dimension | Score (1–5) | Notes |
|---|---|---|
| Structure (As a/I want/So that) | X | brief note |
| Acceptance Criteria | X | brief note |
| Clarity (no vague language) | X | brief note |
| Scope (single responsibility) | X | brief note |
| Business Value | X | brief note |
| Testability | X | brief note |
| **Overall** | **X.X/5** | **READY / NEEDS WORK / BLOCKED** |

Overall verdict rules: READY = avg ≥ 4.0 and no Critical issues. NEEDS WORK = avg 2.5–3.9 or at least one Critical. BLOCKED = avg < 2.5 or AC missing entirely.

---
### Issues Found
[Each issue: **[DIMENSION] [SEVERITY: Critical/Major/Minor]** — description. Quote the exact problematic text where applicable. If none, write "No significant issues found."]

---
### Suggested Improvements
[Numbered concrete rewrites or additions — not generic advice. Reference actual text. If none needed, write "None."]

---
### QA Readiness Assessment
[One paragraph: is this story ready for test case writing? What specific questions must the QA engineer ask the PO before starting?]

Scoring guide: 5=no issues, 4=minor issues, 3=one significant gap, 2=multiple gaps, 1=missing/unclear.
Be specific. Quote actual text. Do not give generic story-writing advice."""

    review = _call_ai(prompt, max_tokens=3500)
    print(f"  Review complete.")
    return review
