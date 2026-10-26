"""
Diagnostic unit tests — run with:
  cd testing-platform/backend && python -m pytest tests/test_config_and_ai.py -v
No API calls made. The unit tests are hermetic: they never read the real .env.
Live checks against the real .env (AIO token expiry, configured AI provider)
run only when RUN_LIVE_ENV_CHECKS=1 is set.
"""
import base64
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make sure backend/ is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _decode_jwt_exp(token: str) -> int | None:
    """Extract 'exp' from a JWT without verifying signature."""
    try:
        raw = token.removeprefix("Bearer ").strip()
        parts = raw.split(".")
        if len(parts) < 2:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(padded))
        return payload.get("exp")
    except Exception:
        return None


# ── 1. validate_all() logic ───────────────────────────────────────────────────

LIVE_ENV_CHECKS = os.getenv("RUN_LIVE_ENV_CHECKS") == "1"
live_env_only = pytest.mark.skipif(
    not LIVE_ENV_CHECKS, reason="checks the real .env; set RUN_LIVE_ENV_CHECKS=1 to run"
)


class TestValidateAll:
    """Tests for config.validate_all() without touching the real env or .env."""

    def _run(self, env_overrides: dict, cli_available: bool = False) -> str | None:
        """Return error message if validate_all() raises, else None."""
        import importlib
        import agent.config as cfg
        try:
            # Reloading re-runs load_dotenv(); stub it so the real .env never
            # leaks into the cleared test environment.
            with patch.dict(os.environ, env_overrides, clear=True), \
                 patch("dotenv.load_dotenv", return_value=False):
                importlib.reload(cfg)
                with patch.object(cfg, "claude_cli_available", return_value=cli_available):
                    try:
                        cfg.validate_all()
                        return None
                    except EnvironmentError as e:
                        return str(e)
        finally:
            # Restore module-level values computed from the real environment.
            importlib.reload(cfg)

    def _base_env(self) -> dict:
        return {
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_EMAIL": "test@example.com",
            "JIRA_API_TOKEN": "token123",
            "AIO_TOKEN": "Bearer eyJ.fake.token",
            "AIO_CSRF": "123456",
            "AIO_SESSION": "abc-def",
        }

    def test_passes_with_valid_anthropic_key(self):
        env = {**self._base_env(), "ANTHROPIC_API_KEY": "sk-ant-abc123"}
        err = self._run(env)
        assert err is None, f"Expected no error, got: {err}"

    def test_passes_with_claude_cli_and_no_key(self):
        err = self._run(self._base_env(), cli_available=True)
        assert err is None, f"Expected no error, got: {err}"

    def test_gemini_key_alone_is_not_enough(self):
        """Gemini is no longer a provider: a Gemini key must not satisfy validation."""
        env = {**self._base_env(), "GEMINI_API_KEY": "AIzaSyABC123"}
        err = self._run(env)
        assert err is not None and "No AI provider" in err

    def test_fails_with_anthropic_placeholder(self):
        env = {**self._base_env(), "ANTHROPIC_API_KEY": "<from console.anthropic.com>"}
        err = self._run(env)
        assert err is not None, "Expected error when only placeholder Anthropic key is set"

    def test_fails_when_no_ai_key(self):
        err = self._run(self._base_env())
        assert err is not None, "Expected error when no AI key is set"

    def test_fails_when_jira_vars_missing(self):
        err = self._run({"ANTHROPIC_API_KEY": "sk-ant-abc123"})
        assert err is not None and "JIRA_BASE_URL" in err


# ── 2. _call_ai() key routing logic ──────────────────────────────────────────

class TestCallAiRouting:
    """Tests that _call_ai routes to the right provider based on key format.

    test_generator does `import config`, which is a different module object from
    `agent.config`, so the key is patched on tg.config.
    """

    def test_anthropic_key_valid_format(self):
        """Key starting with sk-ant- should route to the Anthropic SDK."""
        import agent.test_generator as tg
        anthropic_key = "sk-ant-validkey"
        msg = MagicMock()
        msg.stop_reason = "end_turn"
        msg.content = [type("B", (), {"type": "text", "text": "hello"})()]
        with patch.object(tg.config, "ANTHROPIC_API_KEY", anthropic_key), \
             patch("anthropic.Anthropic") as mock_client, \
             patch.object(tg, "_call_claude_code_cli") as mock_cli:
            stream_cm = mock_client.return_value.messages.stream.return_value
            stream_cm.__enter__.return_value.get_final_message.return_value = msg
            result = tg._call_ai("test prompt", max_tokens=100)
        mock_client.assert_called_once_with(api_key=anthropic_key)
        mock_cli.assert_not_called()
        assert result == "hello"

    def test_non_anthropic_key_falls_through_to_claude_cli(self):
        """A key that doesn't start with sk-ant- must not route to Anthropic;
        GEMINI_API_KEY is ignored."""
        import agent.test_generator as tg
        with patch.object(tg.config, "ANTHROPIC_API_KEY", "AIzaSyWrongField"), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSyCorrect"}), \
             patch("anthropic.Anthropic") as mock_client, \
             patch.object(tg, "_call_claude_code_cli", return_value="cli-out") as mock_cli:
            result = tg._call_ai("test prompt", max_tokens=100)
        mock_client.assert_not_called()
        mock_cli.assert_called_once_with("test prompt", timeout=300, model=None)
        assert result == "cli-out"

    def test_no_key_and_no_cli_raises(self):
        """No valid key and no Claude CLI on PATH should raise EnvironmentError."""
        import agent.test_generator as tg
        with patch.object(tg.config, "ANTHROPIC_API_KEY", "<placeholder>"), \
             patch("shutil.which", return_value=None):
            with pytest.raises(EnvironmentError, match="Claude Code CLI not found"):
                tg._call_ai("test")


# ── 3. AIO token expiry check ─────────────────────────────────────────────────

@live_env_only
class TestAioTokenExpiry:
    """Checks the current AIO token expiry — no API call, just JWT decode."""

    def test_aio_token_not_expired(self):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)
        token = os.getenv("AIO_TOKEN", "")
        if not token:
            pytest.skip("AIO_TOKEN not set in .env")

        exp = _decode_jwt_exp(token)
        if exp is None:
            pytest.skip("Could not decode AIO_TOKEN expiry")

        now = int(time.time())
        remaining_minutes = (exp - now) // 60
        assert exp >= now, (
            f"AIO_TOKEN expired {abs(remaining_minutes)} minutes ago. "
            "Fix: refresh tokens from browser DevTools and paste into .env or admin UI"
        )
        print(f"\nOK: AIO_TOKEN valid for {remaining_minutes} more minutes")


# ── 4. .env AI provider state ─────────────────────────────────────────────────

@live_env_only
class TestEnvAiKeyState:
    """Shows which AI provider the current .env will select."""

    def test_show_ai_provider_selection(self):
        from dotenv import load_dotenv
        import agent.config as cfg
        load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)

        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        has_anthropic = anthropic_key.startswith("sk-ant-")
        has_cli = cfg.claude_cli_available()

        print(f"\nANTHROPIC_API_KEY set: {bool(anthropic_key)}, valid format: {has_anthropic}")
        print(f"Claude Code CLI on PATH: {has_cli}")
        print("=> Will use: " + ("Claude (Anthropic API)" if has_anthropic
                                 else "Claude Code CLI" if has_cli else "NOTHING"))

        assert has_anthropic or has_cli, (
            "No AI provider found. Set ANTHROPIC_API_KEY (sk-ant-...) in .env "
            "or install and log in to the Claude Code CLI."
        )
