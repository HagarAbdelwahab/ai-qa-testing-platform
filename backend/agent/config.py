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
    # At least one AI provider must be configured
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    has_anthropic = bool(anthropic_key) and anthropic_key.startswith("sk-ant-")
    has_gemini = bool(gemini_key) and not gemini_key.startswith("<")
    if not has_anthropic and not has_gemini:
        raise EnvironmentError(
            "\n  No AI provider configured.\n"
            "  Set ANTHROPIC_API_KEY (starts with sk-ant-) or GEMINI_API_KEY in testing-platform/.env\n"
        )


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

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_WEBHOOK = _get("SLACK_WEBHOOK")

# ── Playwright repo ───────────────────────────────────────────────────────────
PLAYWRIGHT_REPO_PATH = _get(
    "PLAYWRIGHT_REPO_PATH",
    str(Path(__file__).parent.parent.parent.parent / "mojsso-playwright"),
)
