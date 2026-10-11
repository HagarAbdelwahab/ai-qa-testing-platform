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
import json
import os
import subprocess
import sys
from pathlib import Path

import config
import jira_client
import aio_client
import test_generator
import bug_reporter


# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(text: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def _norm_title(title: str) -> str:
    """Normalised test case title, for matching against what AIO already holds."""
    return " ".join((title or "").split()).strip().lower()


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

def cmd_kickoff(issue_keys: list[str], cycle_name: str) -> None:
    """
    For each story:
      1. Read Jira story + fetch comments and linked tickets for context
      2. Generate test cases with Claude (using full Jira history)
      3. Create AIO test cases, test set, link to story and cycle
    """
    config.validate_all()
    _banner(f"SPRINT KICKOFF — {len(issue_keys)} stories | Cycle: {cycle_name}")

    # Find or create the AIO cycle (skipped when cycle_name is empty)
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

    for issue_key in issue_keys:
        _process_story_kickoff(issue_key, cycle_id, cycle_name)

    _banner("KICKOFF COMPLETE")
    print("Next step: when a story moves to 'Ready for QA', run:")
    print("  python3 qa_agent.py execute --issue <KEY> --data <csv_file> --cycle \"" + cycle_name + '"')


def _process_story_kickoff(story_key: str, cycle_id: int, cycle_name: str) -> None:
    _banner(f"Processing: {story_key}")
    state = _load_state(story_key)

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

    # ── Step 2: Generate test cases with Claude ───────────────────────────────
    print(f"\n[2/3] Generating test cases with Claude AI")
    # Cache is keyed on the story input: when the description, comments or the
    # extractor change, cached cases are stale and must be regenerated.
    import hashlib
    input_hash = hashlib.sha256(json.dumps(
        [story_title, story_desc, jira_comments, linked_issues], ensure_ascii=False
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
        )
        state["test_cases"] = test_cases
        state["input_hash"] = input_hash
        _save_state(story_key, state)

    print(f"  Test cases: {len(test_cases)}")
    for i, tc in enumerate(test_cases):
        print(f"    {i+1}. {tc['title']}")

    # ── Step 3: Create AIO artifacts ──────────────────────────────────────────
    print(f"\n[3/3] Creating AIO Test Cases, Set, and linking to Cycle")

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

    print(f"\n  {story_key} kickoff complete:")
    print(f"    AIO Cases  : {', '.join(aio_tc_keys)}")
    print(f"    Test Set   : {ts_key}")
    print(f"    Cycle      : {cycle_name} (ID:{cycle_id})")


# ── Phase 2: Test Execution ───────────────────────────────────────────────────

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

    if not state:
        print(f"ERROR: No kickoff state found for {story_key}.")
        print(f"Run kickoff first: python3 qa_agent.py kickoff --issues {story_key} --cycle \"{cycle_name}\"")
        sys.exit(1)

    aio_tc_keys = state.get("aio_tc_keys", [])
    aio_tc_ids = state.get("aio_tc_ids", [])
    test_cases = state.get("test_cases", [])
    cycle_id = state.get("aio_cycle_id")
    spec_path = state.get("playwright_spec_path")
    subtasks = state.get("subtasks", {})

    # Map test title → AIO key for bug reporting
    aio_tc_key_map = {tc["title"]: key for tc, key in zip(test_cases, aio_tc_keys)}

    # ── Handle test data file ─────────────────────────────────────────────────
    playwright_repo = Path(config.PLAYWRIGHT_REPO_PATH)
    if data_file:
        if not os.path.exists(data_file):
            print(f"ERROR: Data file not found: {data_file}")
            sys.exit(1)
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

    pw_cmd = [
        "npx", "playwright", "test", spec_relative,
        "--reporter=json",
        f"--output={results_path.parent}",
    ]

    result = subprocess.run(
        pw_cmd,
        cwd=str(playwright_repo),
        capture_output=False,
    )
    pw_exit_code = result.returncode
    print(f"\n  Playwright exit code: {pw_exit_code} ({'PASSED' if pw_exit_code == 0 else 'FAILURES DETECTED'})")

    # Save JSON results (Playwright writes results.json when using --reporter=json)
    # Playwright actually writes to stdout with json reporter — let's capture it properly
    result2 = subprocess.run(
        ["npx", "playwright", "test", spec_relative, "--reporter=json"],
        cwd=str(playwright_repo),
        capture_output=True,
        text=True,
    )
    if result2.stdout.strip().startswith("{"):
        results_path.write_text(result2.stdout, encoding="utf-8")

    # ── Report bugs for failures ──────────────────────────────────────────────
    print(f"\n[2/3] Analyzing failures and creating Jira bugs")
    bug_keys = []
    if results_path.exists():
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
        print("  No JSON results file found — skipping bug reporting.")

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
    else:
        print(f"  Bugs filed: None (all tests passed!)")
    print(f"  Playwright: {spec_path}")


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
