"""
Playwright failure → Jira bug reporter.
Parses Playwright JSON results and creates Jira bugs for failed tests.
"""
import json
import os
import anthropic
import jira_client
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _ai_analyze_failure(test_name: str, error_message: str,
                         step_title: str, console_logs: str = "") -> dict:
    """
    Use Claude to generate a structured bug analysis from a test failure.
    Returns dict with: summary, root_cause, steps_to_reproduce, severity
    """
    prompt = f"""You are a QA engineer writing a bug report from a failed automated test.

Test name: {test_name}
Error message: {error_message}
Failed at step: {step_title}
Console logs: {console_logs or "None captured"}

Write a concise bug report with:
1. "summary": one-line bug title (max 80 chars)
2. "root_cause": likely technical cause in 1-2 sentences
3. "steps_to_reproduce": numbered list as a single string
4. "severity": one of Critical / High / Medium / Low

Return ONLY valid JSON with those 4 keys."""

    message = _client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def report_failures_from_playwright_results(
    results_json_path: str,
    story_key: str,
    aio_tc_key_map: dict[str, str],  # test title → AIO TC key
    cycle_id: int | None = None,
) -> list[str]:
    """
    Parse Playwright JSON report, create Jira bugs for all failed tests.
    Returns list of created bug keys.

    aio_tc_key_map: maps test title → AIO TC key for cross-linking
    """
    if not os.path.exists(results_json_path):
        print(f"  No results file found at: {results_json_path}")
        return []

    with open(results_json_path) as f:
        results = json.load(f)

    bug_keys = []
    suites = results.get("suites", [])

    for suite in _flatten_suites(suites):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                if test.get("status") not in ("failed", "timedOut"):
                    continue

                test_title = spec.get("title", "Unknown test")
                error_msg = ""
                screenshot_path = None

                # Extract error and screenshot from results
                for result in test.get("results", []):
                    for err in result.get("errors", []):
                        error_msg = err.get("message", "")
                        break
                    for attachment in result.get("attachments", []):
                        if attachment.get("contentType") == "image/png":
                            screenshot_path = attachment.get("path")
                            break

                aio_key = aio_tc_key_map.get(test_title)
                print(f"\n  FAILED: {test_title}")
                print(f"  Error: {error_msg[:120]}...")

                # AI analysis
                analysis = _ai_analyze_failure(
                    test_name=test_title,
                    error_message=error_msg,
                    step_title=test_title,
                )
                print(f"  AI analysis: {analysis.get('severity')} — {analysis.get('summary')}")

                # Build ADF description
                desc_adf = jira_client.build_bug_description_adf(
                    test_name=test_title,
                    error_message=error_msg,
                    failed_step=analysis.get("steps_to_reproduce", test_title),
                    story_key=story_key,
                    aio_tc_key=aio_key,
                )

                # Add AI root cause to description
                desc_adf["content"].insert(2, {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": f"AI Root Cause: {analysis.get('root_cause', '')}"}]
                })

                # Create Jira bug
                bug_key = jira_client.create_bug(
                    parent_key=story_key,
                    title=f"[Automated] {analysis.get('summary', test_title)}",
                    description_adf=desc_adf,
                    screenshot_path=screenshot_path,
                )
                print(f"  Bug created: {bug_key}")
                bug_keys.append(bug_key)

                # Update AIO run status to Failed if cycle provided
                if cycle_id and aio_key:
                    try:
                        import aio_client
                        aio_client.update_test_run_status(
                            config.JIRA_PROJECT_ID, cycle_id, aio_key, status="Failed"
                        )
                        print(f"  AIO {aio_key} marked Failed in cycle {cycle_id}")
                    except Exception as e:
                        print(f"  Warning: could not update AIO run status: {e}")

    return bug_keys


def _flatten_suites(suites: list) -> list:
    """Recursively flatten nested suite structure."""
    result = []
    for suite in suites:
        result.append(suite)
        result.extend(_flatten_suites(suite.get("suites", [])))
    return result


def mark_passed_in_aio(
    results_json_path: str,
    aio_tc_key_map: dict[str, str],
    cycle_id: int,
) -> None:
    """Mark all passing tests as Passed in AIO cycle."""
    if not os.path.exists(results_json_path):
        return

    import aio_client
    with open(results_json_path) as f:
        results = json.load(f)

    for suite in _flatten_suites(results.get("suites", [])):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                if test.get("status") == "passed":
                    title = spec.get("title", "")
                    aio_key = aio_tc_key_map.get(title)
                    if aio_key:
                        aio_client.update_test_run_status(
                            config.JIRA_PROJECT_ID, cycle_id, aio_key, status="Passed"
                        )
                        print(f"  AIO {aio_key} marked Passed")
