"""
Centralized configuration — loads from the platform root .env
(testing-platform/.env)
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load from platform root .env (two levels up from this file: backend/agent/ -> backend/ -> platform root)
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)


def _get(key: str, default: str = "") -> str:
    # AIO tokens: check DB override first (updated via /api/admin/tokens without restart)
    if key in ("AIO_TOKEN", "AIO_CSRF", "AIO_SESSION", "AIO_PAGE_URL", "AIO_PANEL_SELECTOR"):
        try:
            import sys
            from pathlib import Path as _Path
            _backend = str(_Path(__file__).parent.parent)
            if _backend not in sys.path:
                sys.path.insert(0, _backend)
            import db as _db
            v = _db.setting_get(key)
            if v:
                return v
        except Exception:
            pass
    return os.getenv(key, default)


def validate_all() -> None:
    """Fail fast with a clear message if required env vars are missing."""
    required = [
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "AIO_TOKEN", "AIO_CSRF", "AIO_SESSION",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise EnvironmentError(
            f"\n  Missing required env vars: {', '.join(missing)}\n"
            f"  Fill in testing-platform/.env and restart the server.\n"
            f"  AIO tokens must be refreshed from browser DevTools (~45 min expiry)."
        )
    # An AI provider must be configured: an Anthropic API key, or the Claude
    # Code CLI (test_generator._call_ai uses no other provider).
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    has_anthropic = anthropic_key.startswith("sk-ant-")
    if not has_anthropic and not claude_cli_available():
        raise EnvironmentError(
            "\n  No AI provider configured.\n"
            "  Set ANTHROPIC_API_KEY (starts with sk-ant-) in testing-platform/.env,\n"
            "  or install and log in to the Claude Code CLI (`claude` in PATH).\n"
        )


def claude_cli_available() -> bool:
    """True when the Claude Code CLI is on PATH and logged in.

    Uses `claude auth status --json`, which reads local auth state and makes no
    model call. If that command is missing or its output is unreadable (older
    CLI), fall back to the PATH check alone rather than rejecting the CLI.
    """
    import json
    import shutil
    import subprocess
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return False
    # Same env as test_generator's CLI call: a placeholder key is not a login.
    env = os.environ.copy()
    if not env.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"):
        env.pop("ANTHROPIC_API_KEY", None)
    try:
        r = subprocess.run(
            [claude_bin, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=20, env=env,
        )
        status = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return True
    if isinstance(status, dict) and "loggedIn" in status:
        return bool(status["loggedIn"])
    return True


# ── Jira ──────────────────────────────────────────────────────────────────────
JIRA_BASE_URL         = _get("JIRA_BASE_URL", "")
JIRA_EMAIL            = _get("JIRA_EMAIL")
JIRA_API_TOKEN        = _get("JIRA_API_TOKEN")
JIRA_PROJECT_KEY      = _get("JIRA_PROJECT_KEY", "")
JIRA_PROJECT_ID       = int(_get("JIRA_PROJECT_ID", "0") or 0)
JIRA_OWNER_ACCOUNT_ID = _get("JIRA_OWNER_ACCOUNT_ID", "")
JIRA_BOARD_NAME       = _get("JIRA_BOARD_NAME", "")

# Team member first names (lowercase) for column categorisation
def _team(key: str) -> set[str]:
    """Lower-case first names from a comma-separated env var, e.g. QA_TEAM=alice,bob"""
    return {n.strip().lower() for n in _get(key, "").split(",") if n.strip()}


FE_TEAM = _team("FE_TEAM")
BE_TEAM = _team("BE_TEAM")
QA_TEAM = _team("QA_TEAM")
JIRA_PASSWORD         = _get("JIRA_PASSWORD", "")   # for AIO token auto-refresh
AIO_PAGE_URL          = _get("AIO_PAGE_URL", "")    # Jira page that loads AIO TCMS

# ── AIO ───────────────────────────────────────────────────────────────────────
AIO_TOKEN    = _get("AIO_TOKEN")
AIO_CSRF     = _get("AIO_CSRF")
AIO_SESSION  = _get("AIO_SESSION")
AIO_BASE_URL = _get("AIO_BASE_URL", "https://tcms.aiojiraapps.com/aio-tcms/rest/aio-tcms/1.0")
AIO_ORIGIN   = _get("AIO_ORIGIN", "")

# ── Claude ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
# Model for analysis calls (test case generation, test-summary scope). A CLI alias
# (opus/sonnet) or a full model id; aliases map to API ids in test_generator.
ANALYSIS_MODEL    = _get("ANALYSIS_MODEL", "opus")

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_WEBHOOK = _get("SLACK_WEBHOOK")

# ── Context + code repos (story review / reconciliation) ─────────────────────
# Defaults assume the sibling layout: <parent>/{testing-platform,context,backend-repo,frontend-repo}
_REPOS_PARENT = Path(__file__).parent.parent.parent.parent
CONTEXT_REPO_PATH   = _get("CONTEXT_REPO_PATH", str(_REPOS_PARENT / "context"))
CONTEXT_BASE_BRANCH = _get("CONTEXT_BASE_BRANCH", "master")
BE_REPO_PATH        = _get("BE_REPO_PATH", str(_REPOS_PARENT / "backend-repo"))
FE_REPO_PATH        = _get("FE_REPO_PATH", str(_REPOS_PARENT / "frontend-repo"))
CODE_BASE_BRANCH    = _get("CODE_BASE_BRANCH", "production")
# Fetch BE/FE remotes before reconciliation (updates remote refs only, never the checkout).
RECONCILE_FETCH     = _get("RECONCILE_FETCH", "true").lower() == "true"
# Testing-env branches (same names in BE and FE) offered in the Reconcile form.
RECONCILE_ENV_BRANCHES = [b.strip() for b in _get(
    "RECONCILE_ENV_BRANCHES", "qa,preprod").split(",") if b.strip()]

# ── Playwright repo ───────────────────────────────────────────────────────────
PLAYWRIGHT_REPO_PATH = _get(
    "PLAYWRIGHT_REPO_PATH",
    str(Path(__file__).parent.parent.parent.parent / "mojsso-playwright"),
)
