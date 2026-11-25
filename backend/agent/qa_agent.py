#!/usr/bin/env python3
"""
AI QA Orchestration Agent
=========================
Full sprint QA lifecycle automation:
  kickoff  — generates test cases (with Jira history context) + AIO artifacts for all stories in a sprint
  execute  — runs Playwright tests for one story, reports bugs, updates AIO results

USAGE:
  # Sprint starts — process all stories
  python3 qa_agent.py kickoff --issues TAT-100,TAT-101 --cycle "Sprint 15"

  # Story is Ready for QA — run execution
  python3 qa_agent.py execute --issue TAT-100 --data data/tat100.csv --cycle "Sprint 15"

BEFORE RUNNING:
  1. Copy .env.example → .env and fill in all values
  2. Refresh AIO tokens from browser DevTools (expires ~45 min)
"""
import argparse
import contextvars
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

import config
import jira_client
import aio_client
import test_generator
import context_repo
import bug_reporter

try:
    # Present when running inside the backend server (backend/ is on sys.path).
    from jobs import raise_if_cancelled as _raise_if_cancelled
    from jobs import current_job_cancelled as _current_job_cancelled
    from jobs import JobCancelled
except ImportError:  # standalone CLI run: there is no job to cancel
    def _raise_if_cancelled(step: str = "") -> None:
        return None

    def _current_job_cancelled() -> bool:
        return False

    class JobCancelled(Exception):
        pass

# How often a running Playwright process is checked for a UI cancel.
CANCEL_POLL_SECONDS = 5

# Upper bound for one Playwright run; a hung browser must not hold the job forever.
PLAYWRIGHT_TIMEOUT = int(os.getenv("PLAYWRIGHT_TIMEOUT_SECONDS", "3600"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(text: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def _norm_title(title: str) -> str:
    """Normalised test case title, for matching against what AIO already holds.
    Leading tags like "[BR-TK-021]" / "[NO-RULE]" are ignored, so a re-run that adds
    rule prefixes does not duplicate cases created before prefixes existed."""
    title = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", title or "")
    return " ".join(title.split()).strip().lower()


def _load_state(story_key: str) -> dict:
    """Load persisted state for a story (subtask keys, AIO keys, etc.)."""
    state_dir = Path(__file__).parent / ".state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"{story_key}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def _save_state(story_key: str, state: dict) -> None:
    state_dir = Path(__file__).parent / ".state"
    state_dir.mkdir(exist_ok=True)
    with open(state_dir / f"{story_key}.json", "w") as f:
        json.dump(state, f, indent=2)
    print(f"  State saved: .state/{story_key}.json")


# ── Phase 1: Sprint Kickoff ───────────────────────────────────────────────────

def _resolve_cycle(cycle_name: str) -> int | None:
    """Find or create the AIO cycle; None when cycle_name is empty or AIO auth fails."""
    cycle_id = None
    if cycle_name and cycle_name.strip():
        print(f"\n[AIO] Looking for cycle: '{cycle_name}'")
        cycle_id, cycle_title = aio_client.find_cycle_by_name(config.JIRA_PROJECT_ID, cycle_name)
        if cycle_title == "__TOKEN_ERROR__":
            print("  AIO: token expired — refreshing before cycle lookup...")
            if aio_client._try_refresh():
                aio_client._cycle_id_cache.clear()
                cycle_id, cycle_title = aio_client.find_cycle_by_name(config.JIRA_PROJECT_ID, cycle_name)
                if cycle_title == "__TOKEN_ERROR__":
                    print("  AIO: refresh succeeded but cycle lookup still failing — skipping cycle.")
                    cycle_id = None
            else:
                print("  AIO: token refresh failed — skipping cycle linking.")
                cycle_id = None
        if not cycle_id and cycle_title != "__TOKEN_ERROR__":
            print(f"  Cycle not found. Creating: '{cycle_name}'")
            cycle_id, cycle_key = aio_client.create_cycle(
                config.JIRA_PROJECT_ID, config.JIRA_OWNER_ACCOUNT_ID, cycle_name
            )
            print(f"  Created cycle: {cycle_key} (ID:{cycle_id})")
        elif cycle_id:
            print(f"  Found cycle: '{cycle_title}' (ID:{cycle_id})")
    else:
        print("\n[AIO] No cycle name provided — skipping cycle linking.")
    return cycle_id


def cmd_kickoff(issue_keys: list[str], cycle_name: str) -> None:
    """
    For each story:
      1. Read Jira story + fetch comments and linked tickets for context
      2. Generate test cases with Claude (using full Jira history)
      3. Create AIO test cases, test set, link to story and cycle
    """
    config.validate_all()
    _banner(f"SPRINT KICKOFF — {len(issue_keys)} stories | Cycle: {cycle_name}")

    cycle_id = _resolve_cycle(cycle_name)

    for issue_key in issue_keys:
        _raise_if_cancelled(f"story {issue_key}")
        _process_story_kickoff(issue_key, cycle_id, cycle_name)

    _banner("KICKOFF COMPLETE")
    print("Next step: when a story moves to 'Ready for QA', run:")
    print("  python3 qa_agent.py execute --issue <KEY> --data <csv_file> --cycle \"" + cycle_name + '"')


def _create_aio_artifacts(story_key: str, story_title: str, test_cases: list[dict], state: dict,
                          cycle_id: int | None, cycle_name: str,
                          merge_key_map: bool = False) -> tuple[list[str], str]:
    """Create the cases AIO lacks, link + publish them, add to the story's test set and cycle.
    Saves state. Returns (all AIO case keys for the story, test set key)."""
    # Get Jira issue numeric ID (needed for AIO linking and verification)
    issue_data = jira_client.get_issue(story_key)
    jira_numeric_id = int(issue_data["id"])

    # Ask AIO what already exists before creating anything. AIO is the source of
    # truth, not .state/ — the state file is missing on a fresh checkout, and the
    # testware skill creates cases without ever writing it. Reruns used to skip
    # this check and recreate the whole set, which is what duplicated TAT-6681
    # and TAT-6536.
    try:
        existing = aio_client.get_test_cases_for_story(config.JIRA_PROJECT_ID, jira_numeric_id)
    except aio_client.AioUnavailable as exc:
        raise RuntimeError(
            f"Cannot read existing AIO cases for {story_key} ({exc}). Aborting before "
            f"creation — creating blind would duplicate every case for this story."
        ) from exc

    existing_by_title: dict[str, dict] = {}
    for case in existing:
        existing_by_title.setdefault(_norm_title(case["title"]), case)
    print(f"  AIO already has {len(existing)} case(s) linked to {story_key} "
          f"({len(existing_by_title)} unique titles).")

    # Only create titles AIO doesn't already hold; `seen` also collapses
    # duplicates within a single generated batch.
    to_create, skipped, seen = [], [], set()
    for tc in test_cases:
        norm = _norm_title(tc["title"])
        if norm in existing_by_title or norm in seen:
            skipped.append(tc["title"])
            continue
        seen.add(norm)
        to_create.append(tc)

    for title in skipped:
        print(f"  Skipped (already in AIO): {title}")

    aio_tc_keys = [c["key"] for c in existing_by_title.values()]
    aio_tc_ids = [c["id"] for c in existing_by_title.values()]
    new_ids: list[int] = []
    created_by_title: dict[str, str] = {}   # normalised title -> new AIO key

    if not to_create:
        print(f"  Nothing new to create — all {len(test_cases)} generated case(s) already exist.")
    else:
        print(f"  Creating {len(to_create)} new case(s); skipping {len(skipped)} existing.")

    for tc in to_create:
        key, tc_id = aio_client.create_test_case(
            project_id=config.JIRA_PROJECT_ID,
            owner_id=config.JIRA_OWNER_ACCOUNT_ID,
            title=tc["title"],
            precondition=tc["precondition"],
            step=tc["step"],
            test_data=tc["test_data"],
            expected_result=tc["expected_result"],
        )
        print(f"  Created AIO case: {key} — {tc['title']}")
        aio_tc_keys.append(key)
        aio_tc_ids.append(tc_id)
        new_ids.append(tc_id)
        created_by_title[_norm_title(tc["title"])] = key

        # Link to Jira story
        linked = aio_client.link_test_case_to_story(
            config.JIRA_PROJECT_ID, key, jira_numeric_id
        )
        print(f"  Linked {key} → {story_key}: {'OK' if linked else 'FAILED'}")

    # Cases are created as Draft — publish the batch in one call.
    if new_ids:
        published = aio_client.bulk_update_status(
            config.JIRA_PROJECT_ID, new_ids, status="Published"
        )
        print(f"  Published {len(new_ids)} case(s): {'OK' if published else 'FAILED'}")

    # Reuse the story's existing test set rather than creating a second one
    ts_key = state.get("aio_test_set_key")
    ts_id = state.get("aio_test_set_id")
    if ts_key and ts_id:
        print(f"  Reusing Test Set: {ts_key}")
        if new_ids:
            ok = aio_client.add_cases_to_set(config.JIRA_PROJECT_ID, ts_id, new_ids)
            print(f"  Added {len(new_ids)} new case(s) to set {ts_key}: {'OK' if ok else 'FAILED'}")
    else:
        ts_key, ts_id = aio_client.create_test_set(
            config.JIRA_PROJECT_ID,
            config.JIRA_OWNER_ACCOUNT_ID,
            story_title,
        )
        print(f"  Created Test Set: {ts_key}")
        ok = aio_client.add_cases_to_set(config.JIRA_PROJECT_ID, ts_id, aio_tc_ids)
        print(f"  Added {len(aio_tc_ids)} cases to set {ts_key}: {'OK' if ok else 'FAILED'}")

    state["aio_tc_keys"] = aio_tc_keys
    state["aio_tc_ids"] = aio_tc_ids
    # Title -> key by matching, not by position: aio_tc_keys lists existing
    # cases first, so zipping it against test_cases misattributes results.
    key_map = {
        tc["title"]: (existing_by_title.get(_norm_title(tc["title"])) or {}).get("key")
                     or created_by_title.get(_norm_title(tc["title"]))
        for tc in test_cases
    }
    state["aio_tc_key_map"] = {**state.get("aio_tc_key_map", {}), **key_map} if merge_key_map else key_map
    state["aio_test_set_key"] = ts_key
    state["aio_test_set_id"] = ts_id
    _save_state(story_key, state)

    # Add cases to cycle (only when a cycle was resolved)
    if cycle_id:
        ok = aio_client.add_cases_to_cycle(config.JIRA_PROJECT_ID, cycle_id, aio_tc_ids)
        print(f"  Added cases to cycle '{cycle_name}': {'OK' if ok else 'FAILED'}")
        state["aio_cycle_id"] = cycle_id
        state["aio_cycle_name"] = cycle_name
        _save_state(story_key, state)
    else:
        print("  No cycle — skipping cycle linking.")
    return aio_tc_keys, ts_key


def _generate_story_cases(story_key: str, state: dict) -> tuple[str, list[dict]]:
    """Read the story (description, every comment oldest first, linked tickets, context-repo
    rules) and generate its test cases — cached on the story input. No AIO writes.
    Returns (story_title, test_cases)."""

    # ── Step 1: Read Jira story + history ────────────────────────────────────
    print(f"\n[1/3] Reading Jira story: {story_key}")
    story_title = jira_client.get_issue_summary(story_key)
    story_desc = jira_client.get_issue_description(story_key)
    print(f"  Title: {story_title}")

    jira_comments = jira_client.get_issue_comments(story_key)
    linked_issues = jira_client.get_linked_issues(story_key)
    if jira_comments:
        print(f"  Comments fetched: {len(jira_comments)}")
    if linked_issues:
        print(f"  Linked tickets fetched: {len(linked_issues)}")

    rule_modules, business_rules, rules_error = context_repo.load_story_rules(
        story_key, "\n".join([story_title, story_desc, *jira_comments]))
    # Context is optional: not every module has been pulled into the context repo yet.
    if rules_error:
        print(f"  ⚠️ {rules_error} Generating test cases from the story alone.")
    else:
        print(f"  Business rules: {', '.join(m['id'] for m in rule_modules)} (context branch {story_key})")

    # ── Step 2: Generate test cases with Claude ───────────────────────────────
    print(f"\n[2/3] Generating test cases with Claude AI")
    # Cache is keyed on the story input: when the description, comments or the
    # extractor change, cached cases are stale and must be regenerated.
    import hashlib
    input_hash = hashlib.sha256(json.dumps(
        [story_title, story_desc, jira_comments, linked_issues, business_rules], ensure_ascii=False
    ).encode()).hexdigest()
    if "test_cases" in state and state.get("input_hash") == input_hash:
        print(f"  Using {len(state['test_cases'])} cached test cases.")
        test_cases = state["test_cases"]
    else:
        test_cases = test_generator.generate_test_cases(
            story_key=story_key,
            story_title=story_title,
            story_description=story_desc,
            jira_comments=jira_comments,
            linked_issues=linked_issues,
            business_rules=business_rules,
        )
        state["test_cases"] = test_cases
        state["input_hash"] = input_hash
        _save_state(story_key, state)

    print(f"  Test cases: {len(test_cases)}")
    for i, tc in enumerate(test_cases):
        print(f"    {i+1}. {tc['title']}")

    return story_title, test_cases


def _process_story_kickoff(story_key: str, cycle_id: int, cycle_name: str) -> None:
    _banner(f"Processing: {story_key}")
    state = _load_state(story_key)
    story_title, test_cases = _generate_story_cases(story_key, state)

    # ── Step 3: Create AIO artifacts ──────────────────────────────────────────
    _raise_if_cancelled(f"creating AIO artifacts for {story_key}")
    print(f"\n[3/3] Creating AIO Test Cases, Set, and linking to Cycle")

    aio_tc_keys, ts_key = _create_aio_artifacts(story_key, story_title, test_cases, state,
                                                cycle_id, cycle_name)

    print(f"\n  {story_key} kickoff complete:")
    print(f"    AIO Cases  : {', '.join(aio_tc_keys)}")
    print(f"    Test Set   : {ts_key}")
    print(f"    Cycle      : {cycle_name} (ID:{cycle_id})")


# ── Phase 2: Test Execution ───────────────────────────────────────────────────

def cmd_draft(issue_keys: list[str]) -> list[dict]:
    """Generate test cases for review — nothing is written to AIO. Cases AIO already holds
    for the story are left out. The reviewed cases go to AIO through cmd_add_cases.
    Returns [{"issue_key", "story_title", "cases", "existing"}] per story."""
    config.validate_all()
    _banner(f"DRAFT TEST CASES — {len(issue_keys)} stories (review before AIO)")
    drafts = []
    for story_key in issue_keys:
        _raise_if_cancelled(f"story {story_key}")
        _banner(f"Processing: {story_key}")
        state = _load_state(story_key)
        story_title, test_cases = _generate_story_cases(story_key, state)
        existing = []
        try:
            story_id = int(jira_client.get_issue(story_key)["id"])
            existing = aio_client.get_test_cases_for_story(config.JIRA_PROJECT_ID, story_id)
        except aio_client.AioUnavailable as exc:
            print(f"  ⚠️ Could not read existing AIO cases ({exc}) — showing all; "
                  f"adding still skips duplicates.")
        have = {_norm_title(c["title"]) for c in existing}
        new = [tc for tc in test_cases if _norm_title(tc["title"]) not in have]
        print(f"\n  {story_key}: {len(new)} new case(s) to review; "
              f"{len(test_cases) - len(new)} already in AIO ({len(existing)} linked).")
        drafts.append({"issue_key": story_key, "story_title": story_title, "cases": new,
                       "existing": [{"key": c["key"], "title": c["title"]} for c in existing]})
    return drafts


def cmd_add_cases(story_key: str, cases: list[dict], cycle_name: str) -> dict[str, str]:
    """Add hand-picked test cases (e.g. from a reconciliation) to AIO for an existing story:
    create the ones AIO lacks, link, publish, add to the story's test set and cycle.
    Returns {title: AIO key} for the given cases."""
    config.validate_all()
    _banner(f"ADD CASES — {story_key} | {len(cases)} case(s) | Cycle: {cycle_name or '(none)'}")
    cycle_id = None
    if cycle_name:
        cycle_id = _resolve_cycle(cycle_name)
        if not cycle_id:
            raise RuntimeError(f"AIO cycle '{cycle_name}' could not be found or created — no cases added.")
    state = _load_state(story_key)
    story_title = jira_client.get_issue_summary(story_key)
    _create_aio_artifacts(story_key, story_title, cases, state, cycle_id, cycle_name, merge_key_map=True)
    # Keep the cached case list in step so `execute` covers the added cases too.
    known = {_norm_title(tc["title"]) for tc in state.get("test_cases", [])}
    state["test_cases"] = state.get("test_cases", []) + [
        tc for tc in cases if _norm_title(tc["title"]) not in known]
    _save_state(story_key, state)
    return {tc["title"]: state["aio_tc_key_map"].get(tc["title"]) for tc in cases}


def cmd_execute(story_key: str, data_file: str | None, cycle_name: str) -> None:
    """
    1. Load state from kickoff (AIO keys, subtask keys, spec path)
    2. Optionally copy test data file to Playwright repo
    3. Run Playwright tests
    4. Report failures as Jira bugs
    5. Update AIO run statuses (Passed / Failed)
    6. Mark "Test Execution" subtask as Done
    """
    config.validate_all()
    _banner(f"EXECUTION — {story_key} | Cycle: {cycle_name}")
    state = _load_state(story_key)

    # Raise, never sys.exit(): this runs in a server thread, and SystemExit
    # escapes bridge's `except Exception`, leaving the job "running" forever.
    if not state:
        raise RuntimeError(
            f"No kickoff state found for {story_key}. Run kickoff first: "
            f"python3 qa_agent.py kickoff --issues {story_key} --cycle \"{cycle_name}\""
        )

    aio_tc_keys = state.get("aio_tc_keys", [])
    aio_tc_ids = state.get("aio_tc_ids", [])
    test_cases = state.get("test_cases", [])
    cycle_id = state.get("aio_cycle_id")
    spec_path = state.get("playwright_spec_path")
    subtasks = state.get("subtasks", {})

    # Map test title → AIO key for bug reporting. Older state files predate
    # aio_tc_key_map; rebuild it from AIO by title rather than zipping by position.
    aio_tc_key_map = state.get("aio_tc_key_map")
    if not aio_tc_key_map:
        jira_numeric_id = int(jira_client.get_issue(story_key)["id"])
        by_title = {
            _norm_title(c["title"]): c["key"]
            for c in aio_client.get_test_cases_for_story(config.JIRA_PROJECT_ID, jira_numeric_id)
        }
        aio_tc_key_map = {tc["title"]: by_title.get(_norm_title(tc["title"])) for tc in test_cases}

    # ── Handle test data file ─────────────────────────────────────────────────
    playwright_repo = Path(config.PLAYWRIGHT_REPO_PATH)
    if data_file:
        if not os.path.exists(data_file):
            raise FileNotFoundError(f"Data file not found: {data_file}")
        data_dest = playwright_repo / "data" / os.path.basename(data_file)
        data_dest.parent.mkdir(exist_ok=True)
        import shutil
        shutil.copy(data_file, data_dest)
        print(f"  Test data copied to: {data_dest}")
    else:
        print("  No test data file provided. Running with any existing data.")

    # ── Run Playwright ────────────────────────────────────────────────────────
    results_path = playwright_repo / "test-results" / "results.json"
    results_path.parent.mkdir(exist_ok=True)

    spec_relative = os.path.relpath(spec_path, playwright_repo) if spec_path else "tests/generated/"

    print(f"\n[1/3] Running Playwright tests")
    print(f"  Spec: {spec_relative}")
    print(f"  Results will be at: {results_path}")

    # The list reporter streams progress to the console while the run is going;
    # the json reporter writes the full report to results_path (Playwright reads
    # PLAYWRIGHT_JSON_OUTPUT_FILE).
    pw_cmd = [
        "npx", "playwright", "test", spec_relative,
        "--reporter=list,json",
        f"--output={results_path.parent}",
    ]
    pw_env = {**os.environ, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(results_path)}

    # Never analyse a results file left over from an earlier run.
    results_path.unlink(missing_ok=True)

    _raise_if_cancelled("the Playwright run")

    # One run, in its own process group, so a timeout stops the browsers too.
    try:
        pw_exit_code = _run_playwright(pw_cmd, str(playwright_repo), pw_env, PLAYWRIGHT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Playwright run exceeded {PLAYWRIGHT_TIMEOUT}s and was stopped. "
            f"No results were analysed and no bugs were filed."
        ) from exc

    report = None
    if results_path.exists():
        try:
            report = json.loads(results_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  Could not read Playwright JSON results: {exc}")
    if isinstance(report, dict):
        _print_playwright_summary(report)
    else:
        report = None

    print(f"\n  Playwright exit code: {pw_exit_code} ({'PASSED' if pw_exit_code == 0 else 'FAILURES DETECTED'})")

    # ── Report bugs for failures ──────────────────────────────────────────────
    _raise_if_cancelled("bug reporting")
    print(f"\n[2/3] Analyzing failures and creating Jira bugs")
    bug_keys = []
    fresh_results = report is not None
    if fresh_results:
        bug_keys = bug_reporter.report_failures_from_playwright_results(
            results_json_path=str(results_path),
            story_key=story_key,
            aio_tc_key_map=aio_tc_key_map,
            cycle_id=cycle_id,
        )
        bug_reporter.mark_passed_in_aio(
            results_json_path=str(results_path),
            aio_tc_key_map=aio_tc_key_map,
            cycle_id=cycle_id,
        )
    else:
        print("  Playwright produced no JSON results in this run — "
              "skipping bug reporting and AIO status updates.")

    if bug_keys:
        print(f"\n  Bugs created: {', '.join(bug_keys)}")
        state["bugs_created"] = bug_keys
        _save_state(story_key, state)

    # ── Mark "Test Execution" subtask as Done ─────────────────────────────────
    print(f"\n[3/3] Updating Jira subtask status")
    execution_subtask = subtasks.get("[Testing] Test case execution - E2E")
    if execution_subtask:
        print(f"  Marking '{execution_subtask}' (Test case execution - E2E) → Done")
        jira_client.transition_to_done(execution_subtask)

    _banner("EXECUTION COMPLETE")
    story_url = f"{config.JIRA_BASE_URL}/browse/{story_key}"
    print(f"  Story     : {story_url}")
    print(f"  AIO Cases : {', '.join(aio_tc_keys)}")
    if bug_keys:
        print(f"  Bugs filed: {', '.join(bug_keys)}")
    elif not fresh_results:
        print(f"  Bugs filed: None (no Playwright results to analyse)")
    else:
        print(f"  Bugs filed: None (all tests passed!)")
    print(f"  Playwright: {spec_path}")


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill a process and everything it started (npx -> node -> browsers)."""
    if os.name == "posix":
        try:
            # start_new_session=True made the child a group leader: pgid == pid.
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()


def _run_playwright(cmd: list[str], cwd: str, env: dict, timeout: float) -> int:
    """Run Playwright, streaming its output to the console line by line.

    Returns the exit code. On timeout the whole process group is killed and
    subprocess.TimeoutExpired is re-raised.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=(os.name == "posix"),
    )

    def _pump():
        for line in proc.stdout:
            print(line, end="", flush=True)

    # copy_context keeps the JobCapture binding, so lines reach the job log.
    reader = threading.Thread(target=contextvars.copy_context().run, args=(_pump,), daemon=True)
    reader.start()
    import time
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                return proc.wait(timeout=min(CANCEL_POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                pass
            if _current_job_cancelled():
                raise JobCancelled("Job cancelled during the Playwright run.")
    except BaseException:
        # Timeout, cancel, Ctrl-C or anything else: the browsers must not
        # outlive us (start_new_session took them out of the terminal's group).
        _kill_process_group(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        reader.join(timeout=5)


def _print_playwright_summary(report: dict) -> None:
    """Print a readable summary of a Playwright JSON report for the job console."""
    stats = report.get("stats") or {}
    print(f"  Results: {stats.get('expected', 0)} passed, {stats.get('unexpected', 0)} failed, "
          f"{stats.get('flaky', 0)} flaky, {stats.get('skipped', 0)} skipped")

    def walk(suite, path):
        title = suite.get("title") or ""
        path = path + [title] if title else path
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                status = test.get("status")  # expected | unexpected | flaky | skipped
                if status in ("unexpected", "flaky"):
                    label = "FAILED" if status == "unexpected" else "FLAKY"
                    print(f"    {label}: {' > '.join(path + [spec.get('title', '')])}")
        for child in suite.get("suites", []):
            walk(child, path)

    for suite in report.get("suites", []):
        walk(suite, [])
    for err in report.get("errors", []) or []:
        print(f"    ERROR: {(err.get('message') or '').strip()[:500]}")


# ── Interactive prompts ───────────────────────────────────────────────────────

def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"\n{question}{suffix}: ").strip()
    return val or default


def _prompt_issues() -> list[str]:
    print("\nEnter the Jira issue keys to include in this kickoff.")
    print("You can enter them comma-separated or one per line (blank line to finish).")
    raw = input("  Issue keys (e.g. TAT-100,TAT-101): ").strip()
    if "," in raw:
        return [k.strip() for k in raw.split(",") if k.strip()]
    # one per line fallback
    keys = [raw] if raw else []
    while True:
        line = input("  Add another key (or press Enter to finish): ").strip()
        if not line:
            break
        keys.append(line)
    return keys


def cmd_kickoff_interactive() -> None:
    """Interactive kickoff — prompts for cycle name and issues, then runs."""
    config.validate_all()

    print("\n" + "="*60)
    print("  AI QA AGENT — SPRINT KICKOFF")
    print("="*60)

    # Ask for cycle name
    cycle_name = _prompt("Sprint / Cycle name (will be created in AIO if it doesn't exist)")
    if not cycle_name:
        print("ERROR: Cycle name is required.")
        sys.exit(1)

    # Ask for issue keys
    issue_keys = _prompt_issues()
    if not issue_keys:
        print("ERROR: At least one issue key is required.")
        sys.exit(1)

    print(f"\n  Cycle  : {cycle_name}")
    print(f"  Issues : {', '.join(issue_keys)}")
    confirm = input("\n  Proceed? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        sys.exit(0)

    cmd_kickoff(issue_keys, cycle_name)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AI QA Orchestration Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # kickoff — all args optional; missing ones are prompted interactively
    p_kickoff = sub.add_parser("kickoff", help="Sprint kickoff: generate test cases + AIO artifacts")
    p_kickoff.add_argument("--issues", default=None,
                           help="Comma-separated Jira issue keys (prompted if omitted)")
    p_kickoff.add_argument("--cycle", default=None,
                           help="AIO Test Cycle name (prompted if omitted)")

    # execute
    p_exec = sub.add_parser("execute", help="Execute tests for a story and report results")
    p_exec.add_argument("--issue", required=True, help="Jira issue key, e.g. TAT-100")
    p_exec.add_argument("--data", default=None,
                        help="Path to test data CSV file (optional)")
    p_exec.add_argument("--cycle", required=True, help="AIO Test Cycle name")

    # watch — polls Jira for tickets moving to Ready for QA
    p_watch = sub.add_parser("watch", help="Poll Jira for 'Ready for QA' tickets and run tests")
    p_watch.add_argument("--cycle", required=True, help="AIO Test Cycle name to use for results")
    p_watch.add_argument("--interval", type=int, default=60,
                         help="Poll interval in seconds (default: 60)")
    p_watch.add_argument("--status", default="Ready for QA",
                         help="Jira status name to watch for (default: 'Ready for QA')")

    args = parser.parse_args()

    if args.command == "kickoff":
        if args.issues and args.cycle:
            # Non-interactive: all args provided
            issues = [k.strip() for k in args.issues.split(",")]
            cmd_kickoff(issues, args.cycle)
        else:
            # Interactive: prompt for missing values
            cmd_kickoff_interactive()

    elif args.command == "execute":
        cmd_execute(args.issue, args.data, args.cycle)

    elif args.command == "watch":
        from jira_watcher import cmd_watch
        cmd_watch(args.cycle, args.interval, args.status)


if __name__ == "__main__":
    main()
