"""
Story Review Agent — generates a QA-lens review of a Jira user story BEFORE development.
Fetches story details + comments, loads the AS-IS rules of the affected module(s) from the
context repo, then calls Claude to produce a structured review. The rule changes (🕓) from the
review are applied to modules/<m>/rules.md on the ticket's branch in the context repo, like a code
change: the branch's diff vs master is the story's rule change.
"""
import re

import requests
from requests.auth import HTTPBasicAuth
import config
import context_repo

MAX_CONTEXT_MODULES = 2
HTTP_TIMEOUT = 60  # seconds; a hung Jira call must not block the job forever
RULE_CHANGES_BLOCK_RE = re.compile(r"```rule-changes[ \t]*(\w+)?\s*\n(.*?)```", re.S)


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
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("comments", [])


def _load_context(text: str, issue_key: str = "") -> tuple[list[dict], str, str]:
    """Return (matched modules, context block for the prompt, warning). Never raises.
    Reads the ticket's branch when it exists (a re-review sees its earlier rule changes), else master."""
    try:
        ref = issue_key if issue_key and context_repo.branch_exists(issue_key) else None
        modules = context_repo.detect_modules(text, context_repo.load_module_map(ref))
    except context_repo.ContextRepoError as exc:
        return [], "", f"Context repo unavailable — review ran without AS-IS rules: {exc}"
    if not modules:
        return [], "", "No module in _module-map.md matched this story — review ran without AS-IS rules."
    modules = modules[:MAX_CONTEXT_MODULES]
    blocks = []
    for m in modules:
        m["next_rule"] = context_repo.next_rule_number(m)
        blocks.append(
            f"##### MODULE {m['id']} ({m['folder']}) — matched on: {', '.join(m['matched_keywords'])}\n"
            f"Next free rule ID: BR-{m['id']}-{m['next_rule']:03d}\n\n"
            + context_repo.load_module_context(m, ref)
        )
    return modules, "\n\n".join(blocks), ""


def extract_rule_changes(review: str) -> dict[str, list[dict]]:
    """Map module ID -> [{change, id, rule, source}] from ```rule-changes <ID> blocks."""
    out = {}
    for module_id, body in RULE_CHANGES_BLOCK_RE.findall(review):
        rows = []
        for line in body.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0].upper() in context_repo.CHANGE_KINDS \
                    and context_repo.RULE_ROW.match(f"| {cells[1]} |"):
                rows.append({"change": cells[0].upper(), "id": cells[1], "rule": cells[2],
                             "source": cells[3] if len(cells) > 3 else ""})
        if module_id and rows:
            out[module_id] = rows
    return out


def _write_rule_changes(issue_key: str, story_title: str, modules: list[dict],
                        changes: dict[str, list[dict]]) -> list[str]:
    """Apply each module's rule changes to rules.md on context branch <issue_key> and commit.
    Returns status lines."""
    notes = []
    for m in modules:
        rows = changes.get(m["id"])
        if not rows:
            continue
        path = context_repo.rules_path(m)
        try:
            ref = issue_key if context_repo.branch_exists(issue_key) else None
            current = context_repo.read_file(path, ref)
            if current is None:
                notes.append(f"Could not write rule changes for `{m['id']}`: {path} not found")
                continue
            new, skipped = context_repo.apply_rule_changes(current, rows, issue_key, story_title)
            notes += [f"`{m['id']}`: {s}" for s in skipped]
            if new == current:
                notes.append(f"Rule changes for `{m['id']}` already on context branch `{issue_key}`.")
                continue
            sha = context_repo.commit_file_to_branch(
                issue_key, path, new, f"planned: {issue_key} {m['id']} rule changes from story review")
            counts = ", ".join(f"{k} {n}" for k in context_repo.CHANGE_KINDS
                               if (n := sum(r["change"] == k for r in rows)))
            notes.append(f"Rule changes for `{m['id']}` ({counts}) committed to `{path}` on context branch "
                         f"`{issue_key}` ({sha[:7]}).")
        except context_repo.ContextRepoError as exc:
            notes.append(f"Could not write rule changes for `{m['id']}`: {exc}")
    return notes


def generate_story_review(issue_key: str) -> str:
    from jira_client import get_issue
    from test_generator import _call_ai

    print(f"  [1/4] Reading story: {issue_key}")
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

    print(f"  [2/4] Fetching comments...")
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

    print(f"  [3/4] Loading AS-IS context...")
    modules, context_block, context_warning = _load_context(
        f"{story_title}\n{description}\n{comments_text}", issue_key)
    if modules:
        print(f"  Modules: {', '.join(m['id'] for m in modules)}")
    else:
        print(f"  {context_warning}")

    if modules:
        module_ids = ", ".join(m["id"] for m in modules)
        context_section = f"""
--- AS-IS BUSINESS CONTEXT (production behaviour, from the context repo) ---
Rule markers: ☐ built, ☑ approved, 🕓 planned, ❓ question, ⚠ suspected bug.
{context_block}
--- END AS-IS CONTEXT ---
"""
        context_output = f"""
---
### AS-IS Impact
**Modules:** {module_ids}

#### Existing rules touched by this story (🔁 regression scope)
| Rule ID | Current rule (short) | What the story changes | Impact (Changed / Removed / Regression only) |
|---|---|---|---|
[Only rules the story changes or could break. Cite exact Rule IDs from the context. If none, one row "None".]

#### Contradictions with AS-IS
[Each: story text (quoted) vs Rule ID / flow / status it contradicts, and why it matters. If none, "None found."]

#### Known bugs in this area
[For each ⚠ item from the context that this story touches or should address, a block a manual QA can follow
to reproduce it before reporting — business language, no code. If none, "None."]
**<ID> — <one-line title>**
- **Where:** screen / module and user role
- **Preconditions:** account, data and state needed
- **Steps to reproduce:**
  1. …
  2. …
- **Expected:** what the rule says should happen (cite the Rule ID)
- **Actual:** what the context says happens instead
- **Unknown:** any detail the context does not give (e.g. exact screen, data) — never invent it

#### Open questions in this area
[❓ items from the context that this story touches or should address, with their IDs. If none, "None."]

#### Rule changes
The story's rule changes are applied to rules.md like a code change. One fenced block per module,
exactly in this form (it is parsed and committed automatically):
```rule-changes <MODULE ID>
| Change | Rule ID | Rule | Source |
|---|---|---|---|
| CHANGE | BR-<MODULE ID>-<existing number> | the FULL new text of the existing rule, business language | AC / comment |
| REMOVE | BR-<MODULE ID>-<existing number> | why the rule no longer applies | AC / comment |
| ADD | BR-<MODULE ID>-<next number> | new rule in business language | AC / comment |
```
- CHANGE when the story alters an existing rule: keep its ID, write the whole rule as it will be after the story.
- REMOVE when the story drops an existing rule. Never create a new ID that "replaces" an existing one.
- ADD only for behaviour no existing rule covers. Number from the "Next free rule ID" of that module, upwards.
- Rows whose evidence starts with "{issue_key} ·" are this story's changes from an earlier review: to amend
  them, CHANGE them by their ID; do not ADD them again.
If the story changes no rule for a module, omit its block.
"""
    else:
        context_section = ""
        context_output = ""

    print(f"  [4/4] Generating review with AI...")

    prompt = f"""You are a senior QA engineer reviewing a Jira user story for testability and completeness,
BEFORE development starts. Where AS-IS context is given, judge the story against how the system
behaves in production today: what it changes, what it contradicts, and what it leaves undefined.

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
{context_section}
Produce a structured QA review in exactly this markdown format:

## User Story Review — {issue_key}

**Summary:** {story_title}
**Status:** {status}

---
### Comments Summary
[Summarise key decisions or open questions from comments. If no comments, write "No comments on this story."]

---
### Verdict
**READY / NEEDS WORK / BLOCKED** — one line on why.

Verdict rules: READY = no Critical issues and at most minor gaps. NEEDS WORK = at least one Critical or Major issue. BLOCKED = AC or story structure missing entirely.

---
### Issues Found
[Dimensions: Structure, Acceptance Criteria, Clarity, Scope, Business Value, Testability. Each issue: **[DIMENSION] [SEVERITY: Critical/Major/Minor]** — description. Quote the exact problematic text where applicable. If none, write "No significant issues found."]

---
### Suggested Improvements
[Numbered concrete rewrites or additions — not generic advice. Reference actual text. If none needed, write "None."]
{context_output}
---
### QA Readiness Assessment
[One paragraph: is this story ready for test case writing? What specific questions must the QA engineer ask the PO before starting?]

Do not give numeric scores.
Be specific. Quote actual text. Do not give generic story-writing advice."""

    review = _call_ai(prompt, max_tokens=8000 if modules else 3500)
    print(f"  Review complete.")

    notes = [context_warning] if context_warning else []
    if modules:
        notes += _write_rule_changes(issue_key, story_title, modules, extract_rule_changes(review))
    for n in notes:
        print(f"  {n}")
    if notes:
        review += "\n\n---\n### Context repo\n" + "\n".join(f"- {n}" for n in notes)
    return review
