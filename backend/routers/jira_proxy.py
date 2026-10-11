"""Jira proxy — /api/jira"""
from fastapi import APIRouter, Query

import bridge

router = APIRouter()


@router.get("/tickets")
def get_tickets(
    jql: str = Query(default="", description="Custom JQL — leave empty for open sprint"),
):
    try:
        return bridge.get_jira_tickets(jql)
    except Exception as exc:
        return {"error": str(exc), "tickets": []}


@router.get("/health")
def config_health():
    return bridge.config_status()


@router.get("/debug-fields/{issue_key}")
def debug_fields(issue_key: str):
    """Return all fields of an issue to help diagnose missing team data."""
    from agent import jira_client as _jira
    issue = _jira.get_issue(issue_key)
    fields = issue.get("fields", {})
    # Show field id -> type + preview for array/user fields
    summary = {}
    for k, v in fields.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "displayName" in v[0]:
            summary[k] = [u.get("displayName") for u in v]
        elif isinstance(v, dict) and "displayName" in v:
            summary[k] = v.get("displayName")
        elif v is not None and not isinstance(v, (dict, list)):
            summary[k] = str(v)[:80]
    return {"issue": issue_key, "multi_assignee_field_detected": _jira._get_multi_assignee_field(), "fields": summary}
