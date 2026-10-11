"""
Agent bridge — imports directly from backend/agent/ (fully self-contained).
No dependency on the external tarekat_ineteg_MCP project.
"""
import sys
import threading
from pathlib import Path

# Add backend/ to path so agent modules can do `import config` etc.
_BACKEND = Path(__file__).parent
_AGENT = _BACKEND / "agent"
for p in [str(_BACKEND), str(_AGENT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from agent import config as _cfg
from agent import jira_client as _jira
from agent import test_generator as _gen
from agent import qa_agent as _agent

_gen_lock = threading.Lock()


# ── Public helpers ────────────────────────────────────────────────────────────

def is_ready() -> bool:
    return True


def config_status() -> dict:
    import os
    required = [
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "AIO_TOKEN", "AIO_CSRF", "AIO_SESSION", "ANTHROPIC_API_KEY",
    ]
    return {
        "mcp_found": True,
        "mcp_path": str(_AGENT),
        "setup_error": "",
        "jira_base_url": _cfg.JIRA_BASE_URL,
        "env": {k: bool(os.getenv(k)) for k in required},
    }


def get_jira_tickets(jql: str = "") -> list[dict]:
    import os
    if not jql:
        pk = os.getenv("JIRA_PROJECT_KEY", "TAT")
        jql = f"project={pk} AND sprint in openSprints() ORDER BY priority DESC"
    raw = _jira.get_issues_by_jql(jql)
    out = []
    for issue in raw:
        f = issue.get("fields", {})
        out.append({
            "key": issue.get("key", ""),
            "summary": f.get("summary", ""),
            "status": f.get("status", {}).get("name", ""),
            "priority": f.get("priority", {}).get("name", "Medium"),
            "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
        })
    return out


def run_kickoff(job_id: str, issues: list[str], cycle_name: str, knowledge_context: str = "") -> None:
    from jobs import JobCapture, complete_job, fail_job
    try:
        with _gen_lock:
            with JobCapture(job_id):
                if knowledge_context:
                    # Patch the module qa_agent actually imported (`test_generator`),
                    # not `agent.test_generator` — they are different module objects,
                    # so patching `_gen` never reached the generator.
                    _tg = _agent.test_generator
                    _original = _tg.generate_test_cases

                    def _gen_with_kb(story_key, story_title, story_description, *args, **kwargs):
                        enhanced = (
                            story_description
                            + "\n\n--- BUSINESS KNOWLEDGE CONTEXT ---\n"
                            + knowledge_context
                        )
                        return _original(story_key, story_title, enhanced, *args, **kwargs)

                    _tg.generate_test_cases = _gen_with_kb

                try:
                    _agent.cmd_kickoff(issues, cycle_name)
                finally:
                    if knowledge_context:
                        _tg.generate_test_cases = _original

        complete_job(job_id, {"issues": issues, "cycle": cycle_name})
    except Exception as exc:
        fail_job(job_id, str(exc))


def run_execute(job_id: str, issue: str, cycle_name: str, data_file: str | None = None) -> None:
    from jobs import JobCapture, complete_job, fail_job
    try:
        with JobCapture(job_id):
            _agent.cmd_execute(issue, data_file, cycle_name)
        complete_job(job_id, {"issue": issue, "cycle": cycle_name})
    except Exception as exc:
        fail_job(job_id, str(exc))


def pull_sprint_stories(sprint_name: str, issue_types: str = "Story") -> tuple[list[dict], str]:
    """Fetch all stories in a sprint — synchronous.
    Pass sprint_name='' or 'current' to load the active open sprint.
    Returns (stories, resolved_sprint_name)."""
    import re
    resolved = sprint_name.strip()
    if not sprint_name or sprint_name.strip().lower() == "current":
        active_sprint = _jira.get_active_sprint_for_board(_cfg.JIRA_BOARD_NAME)
        if active_sprint:
            resolved = active_sprint
            jql = (
                f'project = {_cfg.JIRA_PROJECT_KEY} AND sprint = "{active_sprint}" '
                f'AND issuetype in ({issue_types}) ORDER BY rank ASC'
            )
        else:
            jql = (
                f'project = {_cfg.JIRA_PROJECT_KEY} AND sprint in openSprints() '
                f'AND issuetype in ({issue_types}) ORDER BY rank ASC'
            )
    else:
        jql = (
            f'project = {_cfg.JIRA_PROJECT_KEY} AND sprint = "{sprint_name.strip()}" '
            f'AND issuetype in ({issue_types}) ORDER BY rank ASC'
        )
    raw = _jira.get_issues_by_jql(jql)
    multi_field = _jira._get_multi_assignee_field()
    out = []

    def _classify(users: list[dict], fe: list, be: list, qa: list, seen: set):
        for u in users:
            name = (u.get("displayName") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            # Handle both "Firstname Lastname" and "firstname.lastname" formats
            first = name.split()[0].lower().split(".")[0]
            display = name.split()[0].split(".")[0].capitalize()
            if first in _cfg.QA_TEAM:
                qa.append(display)
            elif first in _cfg.FE_TEAM:
                fe.append(display)
            elif first in _cfg.BE_TEAM:
                be.append(display)

    for issue in raw:
        f = issue.get("fields", {})
        all_users = list(f.get(multi_field, []) or []) if multi_field else []
        if single := f.get("assignee"):
            all_users.append(single)
        fe, be, qa, seen = [], [], [], set()
        _classify(all_users, fe, be, qa, seen)
        out.append({
            "key": issue.get("key", ""),
            "summary": f.get("summary", ""),
            "status": f.get("status", {}).get("name", ""),
            "priority": (f.get("priority") or {}).get("name", "Medium"),
            "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
            "subtasks": len(f.get("subtasks", [])),
            "fe": fe,
            "be": be,
            "qa": qa,
            "_seen": seen,  # carry forward to merge subtask assignees below
        })

    # Enrich with subtask assignees using keys already embedded in the story response
    subtask_to_story: dict[str, str] = {}
    for issue in raw:
        story_key = issue.get("key", "")
        for st in issue.get("fields", {}).get("subtasks", []):
            if st_key := st.get("key", ""):
                subtask_to_story[st_key] = story_key

    if subtask_to_story:
        subtask_map = _jira.get_subtask_assignees(subtask_to_story)
        for story in out:
            extra = subtask_map.get(story["key"], [])
            _classify(extra, story["fe"], story["be"], story["qa"], story["_seen"])

    for story in out:
        del story["_seen"]
    # Derive AIO cycle name: "Sprint 35 Squad-A" → "squad-a-sprint-35"
    cycle_name = ""
    m = re.search(r"\d+", resolved)
    if m:
        cycle_name = f"squad-a-sprint-{m.group()}"
    return out, resolved, cycle_name


def get_cycle_stats(cycle_name: str) -> dict:
    from agent import aio_client as _aio
    return _aio.get_cycle_stats(_cfg.JIRA_PROJECT_ID, cycle_name)


def run_review_story(job_id: str, issue_key: str) -> None:
    from jobs import JobCapture, complete_job, fail_job
    from agent import story_reviewer
    try:
        review_text = None
        with JobCapture(job_id):
            review_text = story_reviewer.generate_story_review(issue_key)
        complete_job(job_id, {"issue_key": issue_key, "review": review_text})
    except Exception as exc:
        fail_job(job_id, str(exc))


def run_create_subtasks(job_id: str, issue_keys: list[str]) -> None:
    from jobs import JobCapture, complete_job, fail_job
    try:
        all_results = {}
        with JobCapture(job_id):
            for key in issue_keys:
                print(f"\n── {key} ──────────────────────────")
                result = _jira.create_subtasks(key)
                all_results[key] = result
        complete_job(job_id, {"results": all_results})
    except Exception as exc:
        fail_job(job_id, str(exc))


def run_test_summary_gather(job_id: str, cycle_name: str, keys: list[str], pull_aio: bool) -> None:
    from jobs import JobCapture, complete_job, fail_job
    from agent import test_summary_report as _tsr
    try:
        with JobCapture(job_id):
            prefill = _tsr.gather(cycle_name, keys, pull_aio)
        complete_job(job_id, {"tsr_prefill": prefill})
    except Exception as exc:
        fail_job(job_id, str(exc))


def run_test_summary_generate(job_id: str, data: dict) -> None:
    from pathlib import Path as _P
    from jobs import JobCapture, complete_job, fail_job
    from agent import test_summary_report as _tsr
    try:
        with JobCapture(job_id):
            print("  Rendering Word and PDF...")
            docx_path, pdf_path = _tsr.render(data)
            print(f"  Saved {docx_path}")
            print(f"  Saved {pdf_path}")
        complete_job(job_id, {"tsr_files": [_P(docx_path).name, _P(pdf_path).name]})
    except Exception as exc:
        fail_job(job_id, str(exc))
