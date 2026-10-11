"""
Jira Status Watcher
===================
Polls Jira for tickets that move to a configured status (default: "Ready for QA").
When a ticket is detected, it:
  1. Notifies you with the ticket details
  2. Asks for your permission to start testing
  3. Asks for the test data file (optional)
  4. Runs the full execute flow

Run this in a terminal while working — it will alert you automatically.

USAGE:
  python3 qa_agent.py watch --cycle "Sprint 15"
  python3 qa_agent.py watch --cycle "Sprint 15" --interval 30 --status "Ready for QA"
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
import jira_client


# ── Persisted set of already-processed tickets ────────────────────────────────

_PROCESSED_FILE = Path(__file__).parent / ".state" / "_watch_processed.json"


def _load_processed() -> set[str]:
    _PROCESSED_FILE.parent.mkdir(exist_ok=True)
    if _PROCESSED_FILE.exists():
        return set(json.loads(_PROCESSED_FILE.read_text()))
    return set()


def _save_processed(keys: set[str]) -> None:
    _PROCESSED_FILE.write_text(json.dumps(sorted(keys)))


# ── Jira polling ──────────────────────────────────────────────────────────────

def _fetch_ready_issues(status: str, project_key: str) -> list[dict]:
    """
    Find issues that are currently in the watched status AND have a kickoff
    state file (meaning they went through kickoff and are ready for execution).
    """
    jql = f'project = "{project_key}" AND status = "{status}" ORDER BY updated DESC'
    try:
        return jira_client.get_issues_by_jql(jql)
    except Exception as e:
        print(f"  [watcher] Jira poll error: {e}")
        return []


def _has_kickoff_state(issue_key: str) -> bool:
    """Check whether this ticket has been through kickoff (state file exists)."""
    state_path = Path(__file__).parent / ".state" / f"{issue_key}.json"
    return state_path.exists()


def _load_state(issue_key: str) -> dict:
    state_path = Path(__file__).parent / ".state" / f"{issue_key}.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {}


# ── Interactive prompt helpers ─────────────────────────────────────────────────

def _notify(issue_key: str, summary: str) -> None:
    print("\n" + "!"*60)
    print(f"  READY FOR QA DETECTED")
    print(f"  Ticket  : {issue_key}")
    print(f"  Summary : {summary}")
    print(f"  Time    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("!"*60)


def _ask_permission(issue_key: str, summary: str) -> bool:
    """Ask the user if they want to start testing this ticket."""
    print(f"\n  Ticket {issue_key} — {summary}")
    print("  AI QA Agent is ready to start automated test execution.")
    while True:
        answer = input("  Start testing now? (y / n / skip): ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            print("  Skipping for now. Will ask again on next poll.")
            return False
        if answer == "skip":
            print("  Marked as skipped — will not ask again this session.")
            return None  # None = skip permanently this session
        print("  Please enter y, n, or skip.")


def _ask_test_data(issue_key: str, test_cases: list[dict]) -> str | None:
    """Show the test cases and ask for a test data file."""
    if test_cases:
        print(f"\n  Test cases for {issue_key}:")
        for i, tc in enumerate(test_cases):
            print(f"    {i+1}. {tc.get('title', 'Untitled')}")

    print(f"\n  Does this story require a test data file? (CSV with credentials, tokens, etc.)")
    answer = input("  Path to test data file (or press Enter to skip): ").strip()
    return answer if answer else None


# ── Main watch loop ───────────────────────────────────────────────────────────

def cmd_watch(cycle_name: str, interval: int, status: str) -> None:
    config.validate_all()

    print("\n" + "="*60)
    print("  AI QA AGENT — WATCHING FOR READY FOR QA TICKETS")
    print("="*60)
    print(f"  Project  : {config.JIRA_PROJECT_KEY}")
    print(f"  Status   : \"{status}\"")
    print(f"  Cycle    : {cycle_name}")
    print(f"  Interval : every {interval}s")
    print(f"\n  Watching... Press Ctrl+C to stop.\n")

    processed = _load_processed()
    skipped_this_session: set[str] = set()

    while True:
        try:
            issues = _fetch_ready_issues(status, config.JIRA_PROJECT_KEY)
            new_issues = [
                i for i in issues
                if i["key"] not in processed
                and i["key"] not in skipped_this_session
                and _has_kickoff_state(i["key"])
            ]

            if new_issues:
                for issue in new_issues:
                    key = issue["key"]
                    summary = issue["fields"].get("summary", "")
                    state = _load_state(key)
                    test_cases = state.get("test_cases", [])

                    _notify(key, summary)
                    decision = _ask_permission(key, summary)

                    if decision is None:
                        # User said "skip" — ignore for this session
                        skipped_this_session.add(key)
                        continue

                    if not decision:
                        # User said "no" — will ask again next poll
                        continue

                    # User approved — ask for test data
                    data_file = _ask_test_data(key, test_cases)

                    # Run execution
                    print(f"\n  Starting execution for {key}...")
                    from qa_agent import cmd_execute
                    try:
                        cmd_execute(
                            story_key=key,
                            data_file=data_file,
                            cycle_name=cycle_name,
                        )
                    except Exception as e:
                        print(f"\n  ERROR during execution of {key}: {e}")

                    # Mark as processed so we don't re-run
                    processed.add(key)
                    _save_processed(processed)

            else:
                # Print a quiet heartbeat every 5 minutes
                now = datetime.now()
                if now.second < interval and now.minute % 5 == 0:
                    print(f"  [{now.strftime('%H:%M')}] Watching... (no new Ready for QA tickets)")

            time.sleep(interval)

        except KeyboardInterrupt:
            print("\n\n  Watcher stopped.")
            break
        except Exception as e:
            print(f"  [watcher] Unexpected error: {e}")
            print(f"  Retrying in {interval}s...")
            time.sleep(interval)
