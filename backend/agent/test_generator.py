"""
AI Test Case Generator — supports Anthropic Claude API, Google Gemini, and Claude Code CLI.
Provider selection priority:
  1. ANTHROPIC_API_KEY (starts with sk-ant-)  → Anthropic SDK
  2. GEMINI_API_KEY                           → Google Gemini (free tier)
  3. Claude Code CLI (`claude` in PATH)       → uses your browser-authenticated Claude account
"""
import json
import os
import config


def _parse_json(raw: str) -> list:
    """Strip markdown fences and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith(("json", "typescript", "ts")):
            raw = raw.split("\n", 1)[1]
        raw = raw.strip()
    return json.loads(raw)


def _call_claude_code_cli(prompt: str, timeout: int = 300) -> str:
    """
    Call Claude via the Claude Code CLI (`claude -p`).
    Works with any Claude.ai account — no API key needed.
    The CLI must be installed and already authenticated (claude login done once).
    """
    import subprocess, shutil
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise EnvironmentError(
            "Claude Code CLI not found in PATH. "
            "Install it with: npm install -g @anthropic-ai/claude-code"
        )
    # Strip invalid ANTHROPIC_API_KEY from subprocess env — the CLI treats any
    # non-empty value as a configured key and fails auth if it's a placeholder.
    env = os.environ.copy()
    api_key = env.get("ANTHROPIC_API_KEY", "")
    if not api_key.startswith("sk-ant-"):
        env.pop("ANTHROPIC_API_KEY", None)

    result = subprocess.run(
        [claude_bin, "-p", "-"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise EnvironmentError(
            f"Claude Code CLI error (exit {result.returncode}): {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def _call_ai(prompt: str, max_tokens: int = 4096) -> str:
    """Call whichever AI provider is configured. Returns raw text response."""
    anthropic_key = config.ANTHROPIC_API_KEY
    gemini_key = os.getenv("GEMINI_API_KEY", "")

    # 1. Prefer Anthropic SDK if key looks valid (must start with sk-ant-)
    if anthropic_key and anthropic_key.startswith("sk-ant-"):
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    # 2. Claude Code CLI (browser-authenticated, no API key needed)
    print("  [claude-cli] using Claude Code CLI (browser auth)...")
    return _call_claude_code_cli(prompt)


def generate_test_cases(
    story_key: str,
    story_title: str,
    story_description: str,
    acceptance_criteria: str = "",
    jira_comments: list = None,
    linked_issues: list = None,
) -> list[dict]:
    history_block = ""
    if jira_comments:
        formatted = "\n".join(f"  - {c}" for c in jira_comments[:8])
        history_block += f"\nJira Discussion / Comments (most recent first):\n{formatted}\n"
    if linked_issues:
        formatted = "\n".join(f"  - {l}" for l in linked_issues[:10])
        history_block += f"\nLinked Tickets:\n{formatted}\n"

    prompt = f"""You are a senior QA engineer. Generate comprehensive test cases for the following Jira user story.

Story Key: {story_key}
Title: {story_title}
Description: {story_description}
Acceptance Criteria: {acceptance_criteria or "Not specified"}
{history_block}
Use the discussion comments and linked tickets above (if any) to understand edge cases, reported bugs, clarifications, and constraints that should be covered by the test cases.

COVERAGE RULES — these are mandatory:
1. Read the WHOLE description: every heading, paragraph, list item, nested sub-item and table row.
2. Before writing, silently enumerate every distinct requirement: each rule, condition, branch,
   error/status code, message text, button/field, state, role, API check and known defect.
3. Every enumerated requirement must be covered by at least one test case. Do not merge
   unrelated requirements into one case, and do not stop early — there is no limit on count.
4. If the description already contains test cases (e.g. a table with TC ids), each one must
   become its own test case, keeping its id at the start of the title (e.g. "UI-01: ...").
5. Quote exact UI/error texts from the description in expected_result — do not paraphrase them.
6. Then add what the story implies but does not list:
   - Happy path (positive scenarios)
   - Negative scenarios (invalid inputs, missing required fields)
   - Edge cases (boundary values, empty states)
   - UI validation (field presence, labels, error messages)
   - Any specific scenarios raised in the comments or linked tickets

Return ONLY a valid JSON array. Each element must have exactly these fields:
- "title": short descriptive test case name
- "precondition": what must be true before the test starts
- "step": the action(s) to perform (single string)
- "test_data": input values or N/A
- "expected_result": what should happen

Example format:
[
  {{
    "title": "Successful login with valid credentials",
    "precondition": "User is on the login page",
    "step": "Enter valid username and password, click Login button",
    "test_data": "Username: valid_user, Password: valid_pass",
    "expected_result": "User is redirected to the dashboard"
  }}
]

Respond with ONLY the JSON array, no explanation."""

    raw = _call_ai(prompt, max_tokens=32000)
    test_cases = _parse_json(raw)
    anthropic_key = config.ANTHROPIC_API_KEY
    provider = "Claude (API)" if anthropic_key and anthropic_key.startswith("sk-ant-") else "Claude (CLI)"
    print(f"  {provider} generated {len(test_cases)} test cases for {story_key}")
    return test_cases


def generate_playwright_spec(
    story_key: str,
    story_title: str,
    story_url: str,
    test_cases: list[dict],
    aio_tc_keys: list[str],
    base_url: str,
) -> str:
    tc_summary = "\n".join(
        f"{i+1}. [{aio_tc_keys[i] if i < len(aio_tc_keys) else 'TC-?'}] {tc['title']}\n"
        f"   Precondition: {tc['precondition']}\n"
        f"   Step: {tc['step']}\n"
        f"   Test Data: {tc['test_data']}\n"
        f"   Expected: {tc['expected_result']}"
        for i, tc in enumerate(test_cases)
    )

    prompt = f"""You are a senior automation engineer using Playwright with TypeScript.

Generate a complete, runnable Playwright .spec.ts file for these test cases.

Jira Story: {story_key} — {story_title}
Jira URL: {story_url}
Base URL: {base_url}
AIO Test Cases:
{tc_summary}

Requirements:
1. Use @playwright/test (import {{ test, expect }} from '@playwright/test')
2. Add a comment header block with the Jira story key, title, URL, and AIO test case keys
3. Each AIO test case becomes one test() block
4. Add a comment above each test with its AIO key: // AIO: TC-XXX
5. Use page.goto(), page.fill(), page.click(), expect() appropriately
6. Use descriptive variable names, not generic ones
7. Add await page.waitForLoadState('networkidle') after navigation
8. Group related tests in a describe() block named after the story title
9. Where exact selectors are unknown, use a TODO comment: // TODO: update selector

Return ONLY the TypeScript code, no explanation."""

    return _call_ai(prompt, max_tokens=8192)
