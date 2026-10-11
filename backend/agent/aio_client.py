"""
AIO Tests API client — refactored from aio_workflow.py into a reusable class.
Tokens expire ~45 min; call aio_client.refresh_tokens() or update .env when needed.
"""
import requests
import config
from retry_utils import retry_on_429

_base = config.AIO_BASE_URL


def _headers() -> dict:
    """Build AIO request headers — always reads live values from DB/env."""
    return {
        "authorization": config._get("AIO_TOKEN"),
        "aio-tests-csrf-token": config._get("AIO_CSRF"),
        "x-aio-tests-forge-session": config._get("AIO_SESSION"),
        "x-aio-tests-platform": "forge",
        "content-type": "application/json",
        "accept": "application/json",
        "origin": config.AIO_ORIGIN,
    }


def _get(path: str) -> dict:
    r = requests.get(f"{_base}/{path}", headers=_headers())
    try:
        d = r.json()
        if r.status_code in (401, 403):
            d["_auth_failed"] = True
        return d
    except Exception:
        return {"_raw": r.text[:300], "_auth_failed": r.status_code in (401, 403)}


def _refresh_and_retry(fn):
    """Refresh AIO tokens then retry fn() once on auth/server error."""
    try:
        from token_refresher import refresh_tokens
        if refresh_tokens():
            return fn()
    except Exception:
        pass
    raise requests.HTTPError("AIO call failed and token refresh did not help")


def _post(path: str, body: dict) -> dict:
    def _do():
        r = requests.post(f"{_base}/{path}", headers=_headers(), json=body)
        if r.status_code in (401, 403, 500):
            return _refresh_and_retry(lambda: _do_raw())
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:300]}

    def _do_raw():
        r = requests.post(f"{_base}/{path}", headers=_headers(), json=body)
        r.raise_for_status()
        return r.json() if r.text else {}

    return retry_on_429(_do, max_retries=4, base_delay=5.0)


def _put(path: str, body: dict) -> dict:
    def _do():
        r = requests.put(f"{_base}/{path}", headers=_headers(), json=body)
        if r.status_code in (401, 403, 500):
            return _refresh_and_retry(lambda: _do_raw())
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:300]}

    def _do_raw():
        r = requests.put(f"{_base}/{path}", headers=_headers(), json=body)
        r.raise_for_status()
        return r.json() if r.text else {}

    return retry_on_429(_do, max_retries=4, base_delay=5.0)


# ── Project metadata (test type / priority / status IDs) ──────────────────────
#
# AIO metadata IDs are assigned per Forge install, so they change whenever the
# Jira site or the AIO app is reinstalled. Never hardcode them — resolve by name
# from project/{id}/config, which is the only metadata endpoint AIO exposes
# (every project/{id}/testtype, /priority, /status variant returns HTTP 500).

_config_cache: dict = {}  # project_id -> config payload


def get_project_config(project_id: int, refresh: bool = False) -> dict:
    """Fetch and cache AIO project config (metadata ID lookup tables)."""
    if refresh or project_id not in _config_cache:
        resp = _get(f"project/{project_id}/config")
        if _is_aio_error(resp) or "testType" not in resp:
            return {}
        _config_cache[project_id] = resp
    return _config_cache.get(project_id, {})


def meta_id(project_id: int, group: str, name: str) -> int | None:
    """Resolve an AIO metadata numeric ID by its display name (case-insensitive).

    group is a config key such as "testType", "testPriority", "testStatus",
    "testScriptType" or "testRunStatus". Falls back to the group's default
    entry, then its first entry, so a renamed option still yields a usable ID.
    """
    items = get_project_config(project_id).get(group) or []
    if not items:
        return None
    needle = (name or "").strip().lower()
    for item in items:
        if (item.get("name") or "").strip().lower() == needle:
            return item.get("ID")
    for item in items:
        if item.get("isDefault"):
            return item.get("ID")
    return items[0].get("ID")


def run_status_id(project_id: int, name: str) -> int | None:
    """Resolve a test run status ID (Not Run / In Progress / Passed / Failed / Blocked)."""
    return meta_id(project_id, "testRunStatus", name)


# ── Test Case ─────────────────────────────────────────────────────────────────

def create_test_case(project_id: int, owner_id: str, title: str,
                     precondition: str, step: str,
                     test_data: str, expected_result: str,
                     test_type: str = "Functional",
                     priority: str = "Medium",
                     status: str = "Draft",
                     script_type: str = "Classic") -> tuple[str, int]:
    """Create a single test case. Returns (key, id)."""
    detail = {
        "title": title,
        "jiraComponentID": [],
        "jiraReleaseID": [],
        "ownedByID": owner_id,
        "customFieldValues": {},
    }
    # Omit any ID that cannot be resolved — AIO rejects the whole request with
    # "No Case Type found with Id: N" rather than ignoring an unknown value.
    for field, group, wanted in (
        ("testTypeID",       "testType",       test_type),
        ("testPriorityID",   "testPriority",   priority),
        ("testStatusID",     "testStatus",     status),
        ("testScriptTypeID", "testScriptType", script_type),
    ):
        resolved = meta_id(project_id, group, wanted)
        if resolved is not None:
            detail[field] = resolved

    resp = _post(f"project/{project_id}/testcase", {
        "ID": None,
        "jiraProjectID": project_id,
        "precondition": precondition,
        "steps": [{
            "stepOrder": 0,
            "step": step,
            "testData": test_data,
            # CASE_EXPECTED_RESULT is in the project's requiredFields — must be non-empty.
            "expectedResult": expected_result,
            "testStepType": "TEXT",
        }],
        "detail": detail,
    })
    key = resp.get("detail", {}).get("key", "ERR")
    tc_id = resp.get("ID", "ERR")
    return key, tc_id


def link_test_case_to_story(project_id: int, tc_key: str, jira_issue_numeric_id: int) -> bool:
    """Link a test case to a Jira story via requirements field."""
    current = _get(f"project/{project_id}/testcase/{tc_key}")
    if "detail" not in current:
        return False
    current["detail"]["requirements"] = [{"requirementID": jira_issue_numeric_id}]
    result = _put(f"project/{project_id}/testcase/{tc_key}", current)
    saved = result.get("detail", {}).get("requirements", [])
    return bool(saved)


def bulk_update_status(project_id: int, tc_ids: list[int],
                       status: str = "Published") -> bool:
    """Bulk-flip test cases to a status. Returns True only if AIO confirms it.

    Cases are created as Draft; nothing else in the pipeline publishes them, so
    this is what takes a batch out of Draft. One call for the whole batch.
    """
    if not tc_ids:
        return True

    status_id = meta_id(project_id, "testStatus", status)
    if status_id is None:
        print(f"  Publish skipped — no '{status}' status in project {project_id} config.")
        return False

    # Every other update* flag must stay False, or the omitted fields are wiped
    # across the whole batch.
    resp = _put(f"project/{project_id}/testcase/bulk/update", {
        "idList": list(tc_ids),
        "updateTestStatusID": True,
        "testStatusID": status_id,
        "updatePrecondition": False,
        "updateOwnedByID": False,
        "updateTestPriorityID": False,
        "updateTestTypeID": False,
        "updateJiraComponentID": False,
        "updateJiraReleaseID": False,
        "updateEstimatedEffort": False,
        "updateTags": False,
        "tagListOperationType": "ADD_TO_EXISTING",
        "componentListOperationType": "ADD_TO_EXISTING",
        "releaseListOperationType": "ADD_TO_EXISTING",
        "updateRequirements": False,
        "requirements": [],
        "requirementListOperationType": "ADD_TO_EXISTING",
        "updateTestAutomationStatusID": False,
        "updateAutomationOwnerID": False,
        "updatedCustomFieldList": [],
        "preconditionAttachments": [],
    })
    if _is_aio_error(resp):
        print(f"  Publish FAILED — AIO said: {resp.get('ERROR_MESSAGE')}")
        return False

    # AIO returns errors as HTTP 200 with an ERROR_MESSAGE body, so a non-error
    # response is not proof. Read one case back and confirm the status stuck.
    check = _get(f"project/{project_id}/testcase/{tc_ids[0]}")
    actual = (check.get("detail") or {}).get("testStatusID")
    if actual != status_id:
        print(f"  Publish FAILED — case {tc_ids[0]} still has testStatusID={actual}, "
              f"expected {status_id}.")
        return False
    return True


class AioUnavailable(RuntimeError):
    """AIO could not be queried, so 'what already exists' is unknown.

    Callers must treat this as a hard stop rather than as 'nothing exists' —
    guessing 'nothing' is what produced duplicate test cases.
    """


_case_scan_cache: dict[int, list[dict]] = {}


def _fetch_case(project_id: int, key: str) -> dict | None:
    """Fetch one case by key. None = no such key. Raises on auth failure."""
    d = _get(f"project/{project_id}/testcase/{key}")
    if _is_token_error(d):
        raise AioUnavailable(d.get("ERROR_MESSAGE", "AIO token expired"))
    if _is_aio_error(d):
        return None  # gap in the key sequence (never created, or deleted)
    detail = d.get("detail") or {}
    return {
        "key": key,
        "id": d.get("ID"),
        "title": (detail.get("title") or "").strip(),
        "requirement_ids": [
            r.get("requirementID")
            for r in (detail.get("requirements") or [])
            if r.get("requirementID")
        ],
    }


def scan_test_cases(project_id: int, project_key: str | None = None,
                    refresh: bool = False, block: int = 120,
                    max_workers: int = 16) -> list[dict]:
    """Enumerate every live test case in the project by probing its key sequence.

    AIO exposes no working list/search endpoint on this install — every
    `project/{id}/testcase` and `/testcase/search` variant returns HTTP 500,
    and the `?requirementID=` filter returns an error payload that silently
    reads as "no cases" — so we probe `{KEY}-TC-{n}` concurrently, the same
    fallback `find_cycle_by_name` uses for cycles. Stops after a full block of
    consecutive misses. Cached per project for the life of the process.

    `block` must stay comfortably larger than the longest run of deleted keys,
    or the scan stops early and under-reports what exists — which would let
    duplicates back in. Deleting a story's cases can leave a gap as long as that
    story's case count, hence 120.
    """
    if not refresh and project_id in _case_scan_cache:
        return _case_scan_cache[project_id]

    from concurrent.futures import ThreadPoolExecutor

    project_key = project_key or config.JIRA_PROJECT_KEY

    def _scan() -> list[dict]:
        found: list[dict] = []
        start = 1
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            while True:
                keys = [f"{project_key}-TC-{n}" for n in range(start, start + block)]
                hits = [c for c in ex.map(lambda k: _fetch_case(project_id, k), keys) if c]
                found.extend(hits)
                if not hits:
                    break   # whole block empty — end of the sequence
                start += block
        return found

    try:
        found = _scan()
    except AioUnavailable:
        # Tokens last ~45 min; refresh once and retry, as _post/_put do.
        if not _try_refresh():
            raise
        found = _scan()

    _case_scan_cache[project_id] = found
    return found


def get_test_cases_for_story(project_id: int, jira_issue_numeric_id: int,
                             refresh: bool = False) -> list[dict]:
    """Fetch all test cases linked to a Jira story.

    Raises AioUnavailable if AIO cannot be reached — callers must not
    interpret failure as "no existing cases".
    """
    cases = scan_test_cases(project_id, refresh=refresh)
    return [c for c in cases if jira_issue_numeric_id in c["requirement_ids"]]


def delete_test_case(project_id: int, tc_key: str) -> bool:
    """Permanently delete a test case from AIO."""
    r = requests.delete(f"{_base}/project/{project_id}/testcase/{tc_key}", headers=_headers())
    ok = r.status_code in (200, 204)
    if ok:
        _case_scan_cache.pop(project_id, None)
    return ok


# ── Test Set ──────────────────────────────────────────────────────────────────

def create_test_set(project_id: int, owner_id: str, title: str) -> tuple[str, int]:
    """Create a test set. Returns (key, id)."""
    resp = _post(f"project/{project_id}/testset/detail", {
        "ID": None,
        "jiraProjectID": project_id,
        "objectiveAttachments": [],
        "detail": {
            "key": None,
            "title": title,
            "objective": None,
            "jiraComponentID": None,
            "jiraReleaseID": None,
            "ownedByID": owner_id,
            "folder": None,
            "createdDate": None,
            "updatedDate": None,
            "tags": [],
            "customFieldValues": {},
        },
    })
    key = resp.get("detail", {}).get("key", "ERR")
    ts_id = resp.get("ID", "ERR")
    return key, ts_id


def add_cases_to_set(project_id: int, test_set_id: int, tc_ids: list[int]) -> bool:
    id_list = [{"ID": tid, "jiraProjectID": project_id} for tid in tc_ids]
    r = _put(f"project/{project_id}/testset/bulk/testcase/add",
             {"testSetID": test_set_id, "idList": id_list})
    return r.get("status") == "SUCCESS"


# ── Test Cycle ────────────────────────────────────────────────────────────────

_cycle_id_cache: dict = {}  # (project_id, cycle_name_lower) -> cycle_id


def _is_aio_error(resp: dict) -> bool:
    return isinstance(resp, dict) and "ERROR_MESSAGE" in resp


def _is_token_error(resp: dict) -> bool:
    """True only for auth failures, not for 'resource not found' errors."""
    if resp.get("_auth_failed"):
        return True
    msg = (resp.get("ERROR_MESSAGE") or "").lower()
    return _is_aio_error(resp) and any(kw in msg for kw in ("expired", "invalid", "unauthorized", "forbidden", "authentication", "session"))


def list_cycles(project_id: int) -> list[dict]:
    """Fetch all test cycles for a project from AIO."""
    resp = _get(f"project/{project_id}/testcycle?startAt=0&size=500")
    if isinstance(resp, list):
        return resp
    return resp.get("data", [])


def find_cycle_by_name(project_id: int, cycle_name: str,
                       id_range: range = range(1, 400)) -> tuple[int | None, str | None]:
    """Find cycle by name using the cycle list API, falling back to concurrent ID probe.
    Returns (id, title), (None, None) if not found, or (None, "__TOKEN_ERROR__") on auth failure.
    """
    if not cycle_name or not cycle_name.strip():
        return None, None
    needle = cycle_name.strip().lower()
    cache_key = (project_id, needle)

    # Try cycle list API first — call _get directly to catch auth failures
    raw = _get(f"project/{project_id}/testcycle?startAt=0&size=500")
    if _is_token_error(raw):
        return None, "__TOKEN_ERROR__"
    cycles = raw if isinstance(raw, list) else raw.get("data", [])
    for c in cycles:
        cid = c.get("ID")
        title = (c.get("detail") or {}).get("title", "")
        if not title:
            title = c.get("title", "")
        if cid and needle in title.lower():
            _cycle_id_cache[cache_key] = cid
            return cid, title

    # Check cache (avoid re-scanning if already found once this session)
    if cache_key in _cycle_id_cache:
        cid = _cycle_id_cache[cache_key]
        d = _get(f"project/{project_id}/testcycle/{cid}/detail")
        if _is_token_error(d):
            del _cycle_id_cache[cache_key]
            return None, "__TOKEN_ERROR__"
        if not _is_aio_error(d):
            return cid, d.get("detail", {}).get("title", "")
        del _cycle_id_cache[cache_key]

    # Concurrent ID probe — the list API is unreliable; probe all IDs in parallel.
    # Only abort on token errors; skip IDs that return "No Cycle found".
    from concurrent.futures import ThreadPoolExecutor

    def _probe(cid: int) -> tuple[int, str | None]:
        d = _get(f"project/{project_id}/testcycle/{cid}/detail")
        if _is_token_error(d):
            return cid, "__TOKEN_ERROR__"
        if _is_aio_error(d):
            return cid, None  # gap in IDs — skip
        return cid, d.get("detail", {}).get("title", "")

    # Cycle IDs are sparse and keep growing (e.g. 732–900 with nothing below), so a
    # fixed range goes stale. Scan upward in blocks; once cycles have been seen, stop
    # after `empty_after_hits` consecutive empty blocks. `max_id` caps the search.
    block, empty_after_hits, max_id = 400, 2, 10000
    start = id_range.start
    seen_any, empty_run = False, 0
    partial: tuple[int, str] | None = None
    with ThreadPoolExecutor(max_workers=20) as pool:
        while start < max_id:
            stop = start + block
            print(f"  [AIO] Scanning cycle IDs {start}–{stop - 1} for '{cycle_name}'...")
            hits = 0
            for cid, title in pool.map(_probe, range(start, stop)):
                if title == "__TOKEN_ERROR__":
                    return None, "__TOKEN_ERROR__"
                if not title:
                    continue
                hits += 1
                t = title.strip().lower()
                if t == needle:                      # exact title wins immediately
                    _cycle_id_cache[cache_key] = cid
                    return cid, title
                if needle in t and partial is None:
                    partial = (cid, title)
            if partial:
                _cycle_id_cache[cache_key] = partial[0]
                return partial
            if hits:
                seen_any, empty_run = True, 0
            elif seen_any:
                empty_run += 1
                if empty_run >= empty_after_hits:
                    break
            start = stop
    return None, None


def _try_refresh() -> bool:
    """Attempt one Playwright token refresh. Returns True on success."""
    try:
        from token_refresher import refresh_tokens
        return refresh_tokens()
    except Exception:
        return False


def _fetch_cycle_stats(project_id: int, cycle_name: str) -> dict:
    """Single attempt at fetching cycle stats — no refresh logic here."""
    cycle_id, title = find_cycle_by_name(project_id, cycle_name)
    if not cycle_id:
        probe = _get(f"project/{project_id}/testcycle?startAt=0&size=1")
        if _is_token_error(probe):
            return {"error": probe.get("ERROR_MESSAGE", "AIO token expired or invalid"), "aio_error": True, "total": 0}
        return {"error": f"Cycle '{cycle_name}' not found", "total": 0}

    if title == "__TOKEN_ERROR__":
        return {"error": "AIO token expired or invalid", "aio_error": True, "total": 0}

    result = get_cycle_summary_report(project_id, cycle_id, title or cycle_name)
    if "error" in result:
        return {**result, "total": result.get("total", 0)}

    return {"cycle_id": cycle_id, "cycle_title": title, **result}


def get_cycle_stats(project_id: int, cycle_name: str) -> dict:
    """Return test case counts by status for a named cycle.
    Auto-refreshes AIO token via Playwright if expired and retries once.
    Status IDs are resolved per project via run_status_id() — they differ per
    AIO install and must not be hardcoded."""
    result = _fetch_cycle_stats(project_id, cycle_name)
    if result.get("aio_error"):
        print("  AIO: token error detected — attempting auto-refresh...")
        if _try_refresh():
            _cycle_id_cache.clear()  # force fresh cycle lookup with new token
            result = _fetch_cycle_stats(project_id, cycle_name)
        else:
            print("  AIO: auto-refresh failed — set JIRA_PASSWORD in .env or refresh manually via /admin")
    return result


def create_cycle(project_id: int, owner_id: str, title: str) -> tuple[int | None, str]:
    """Create a new test cycle. Returns (id, key)."""
    resp = _post(f"project/{project_id}/testcycle/detail", {
        "ID": None,
        "jiraProjectID": project_id,
        "detail": {
            "title": title,
            "objective": None,
            "ownedByID": owner_id,
            "jiraVersionID": None,
            "customFieldValues": {},
        },
    })
    tc_id = resp.get("ID")
    key = resp.get("detail", {}).get("key", "ERR")
    return tc_id, key


def add_cases_to_cycle(project_id: int, cycle_id: int, tc_ids: list[int]) -> bool:
    id_list = [{"ID": tid, "jiraProjectID": project_id} for tid in tc_ids]
    r = _put(f"project/{project_id}/testcycle/bulk/testcase/add",
             {"testCycleID": cycle_id, "idList": id_list})
    return r.get("status") == "SUCCESS"


def update_test_run_status(project_id: int, cycle_id: int,
                           tc_key: str, status_id: int | None = None,
                           status: str = "") -> bool:
    """
    Update execution result for a test case in a cycle.

    Pass status as a name ("Passed" / "Failed" / "Blocked" / "Not Run") — the
    numeric ID is resolved per project. status_id is still accepted for callers
    that already hold a resolved ID.
    """
    if status_id is None:
        status_id = run_status_id(project_id, status)
        if status_id is None:
            print(f"  [AIO] cannot resolve run status '{status}' for project {project_id}")
            return False
    resp = _put(f"project/{project_id}/testcycle/{cycle_id}/testcase/{tc_key}/testrun", {
        "testRunStatusID": status_id,
    })
    return "ID" in resp or "testRunStatusID" in resp


# Status names AIO ships by default; the summary report needs the full objects,
# which project/{id}/config already provides with correct per-install IDs.
_STATUS_NAMES = ["Not Run", "In Progress", "Passed", "Failed", "Blocked"]


def _status_list(project_id: int) -> list[dict]:
    """Build the testRunStatusList for report requests from live project config."""
    statuses = get_project_config(project_id).get("testRunStatus") or []
    return [
        {
            "ID": s.get("ID"),
            "name": s.get("name"),
            "color": s.get("color"),
            "statusType": s.get("statusType"),
            "coverageType": s.get("coverageType"),
        }
        for s in statuses
    ]


def get_cycle_summary_report(project_id: int, cycle_id: int, cycle_title: str) -> dict:
    """Call the AIO report summary generate endpoint and return parsed distribution.

    Returns dict with keys: passed, failed, blocked, not_run, in_progress, total, pass_rate
    or {"error": ..., "aio_error": True} on failure.
    """
    # Primary: GET testcycle/{id}/summary. The POST report endpoint below returns
    # HTTP 500 "unknown error" on this install; kept only as a fallback.
    resp = _get(f"project/{project_id}/testcycle/{cycle_id}/summary")
    if _is_token_error(resp):
        return {"error": resp.get("ERROR_MESSAGE", "AIO token expired"), "aio_error": True}
    if not _is_aio_error(resp) and isinstance(resp.get("summary"), dict):
        return _summarise(project_id, resp["summary"])

    body = {
        "testCycleId": cycle_id,
        "testCycle": {
            "ID": cycle_id,
            "detail": {"title": cycle_title},
        },
        "testRunStatusList": _status_list(project_id),
        "showExecutionBreakup": True,
    }
    resp = _post(f"project/{project_id}/report/testcycle/summary/generate", body)

    if _is_token_error(resp):
        return {"error": resp.get("ERROR_MESSAGE", "AIO token expired"), "aio_error": True}
    if _is_aio_error(resp):
        return {"error": resp.get("ERROR_MESSAGE", "AIO error"), "aio_error": False}

    return _summarise(project_id, resp.get("testCycleSummary", {}))


def _summarise(project_id: int, summary: dict) -> dict:
    """Turn an AIO cycle summary (testRunDistribution keyed by status ID) into counts."""
    dist = summary.get("testRunDistribution") or {}

    # testRunDistribution is keyed by the per-install status ID, so resolve each
    # name to its ID rather than assuming the old 6–10 range.
    def _count(status_name: str) -> int:
        sid = run_status_id(project_id, status_name)
        return int(dist.get(str(sid), 0)) if sid is not None else 0

    counts = {
        "not_run":     _count("Not Run"),
        "in_progress": _count("In Progress"),
        "passed":      _count("Passed"),
        "failed":      _count("Failed"),
        "blocked":     _count("Blocked"),
    }
    total = sum(counts.values())
    pass_rate = round(counts["passed"] / total * 100) if total else 0
    return {**counts, "total": total, "pass_rate": pass_rate}
