"""
Diagnostic unit tests — run with:
  cd testing-platform/backend && python -m pytest tests/test_config_and_ai.py -v
No API calls made; tests validate logic and .env state only.
"""
import base64
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

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

class TestValidateAll:
    """Tests for config.validate_all() without touching real env."""

    def _run(self, env_overrides: dict) -> str | None:
        """Return error message if validate_all() raises, else None."""
        import importlib
        with patch.dict(os.environ, env_overrides, clear=True):
            import agent.config as cfg
            importlib.reload(cfg)
            try:
                cfg.validate_all()
                return None
            except EnvironmentError as e:
                return str(e)

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

    def test_passes_with_gemini_key(self):
        env = {**self._base_env(), "GEMINI_API_KEY": "AIzaSyABC123"}
        err = self._run(env)
        assert err is None, f"Expected no error, got: {err}"

    def test_fails_with_anthropic_placeholder(self):
        env = {**self._base_env(), "ANTHROPIC_API_KEY": "<from console.anthropic.com>"}
        err = self._run(env)
        assert err is not None, "Expected error when only placeholder Anthropic key is set"

    def test_fails_when_no_ai_key(self):
        env = self._base_env()
        err = self._run(env)
        assert err is not None, "Expected error when no AI key is set"

    def test_passes_with_both_keys(self):
        env = {**self._base_env(), "ANTHROPIC_API_KEY": "sk-ant-abc123", "GEMINI_API_KEY": "AIzaSyABC123"}
        err = self._run(env)
        assert err is None, f"Expected no error, got: {err}"


# ── 2. _call_ai() key routing logic ──────────────────────────────────────────

class TestCallAiRouting:
    """Tests that _call_ai routes to the right provider based on key format."""

    def test_anthropic_key_valid_format(self):
        """Key starting with sk-ant- should route to Anthropic."""
        import agent.test_generator as tg
        anthropic_key = "sk-ant-validkey"
        gemini_key = "AIzaSyFallback"
        with patch.dict(os.environ, {"GEMINI_API_KEY": gemini_key}):
            with patch("agent.config.ANTHROPIC_API_KEY", anthropic_key):
                with patch("anthropic.Anthropic") as mock_client:
                    mock_client.return_value.messages.create.return_value.content = [
                        type("obj", (), {"text": '[{"title":"t","precondition":"p","step":"s","test_data":"d","expected_result":"e"}]'})()
                    ]
                    result = tg._call_ai("test prompt", max_tokens=100)
                    mock_client.assert_called_once_with(api_key=anthropic_key)

    def test_google_key_in_anthropic_field_falls_through_to_gemini(self):
        """Google key in ANTHROPIC_API_KEY must NOT route to Anthropic (key doesn't start sk-ant-)."""
        import agent.test_generator as tg
        google_key_in_wrong_field = "AIzaSyWrongField"
        gemini_key = "AIzaSyCorrect"

        called_urls = []

        def fake_urlopen(req, timeout=60):
            called_urls.append(req.full_url)
            class FakeResponse:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def read(self):
                    return json.dumps({
                        "candidates": [{"content": {"parts": [{"text": "[]"}]}}]
                    }).encode()
            return FakeResponse()

        with patch.dict(os.environ, {"GEMINI_API_KEY": gemini_key}):
            with patch("agent.config.ANTHROPIC_API_KEY", google_key_in_wrong_field):
                with patch("urllib.request.urlopen", fake_urlopen):
                    tg._call_ai("test prompt", max_tokens=100)
        assert called_urls, "Expected Gemini to be called"
        assert "generativelanguage" in called_urls[0], f"Unexpected URL: {called_urls[0]}"
        assert gemini_key in called_urls[0]

    def test_no_key_raises(self):
        """No valid key configured should raise EnvironmentError."""
        import agent.test_generator as tg
        import pytest
        with patch("agent.config.ANTHROPIC_API_KEY", "<placeholder>"):
            with patch.dict(os.environ, {}, clear=True):
                try:
                    tg._call_ai("test")
                    assert False, "Should have raised EnvironmentError"
                except EnvironmentError:
                    pass


# ── 3. AIO token expiry check ─────────────────────────────────────────────────

class TestAioTokenExpiry:
    """Checks the current AIO token expiry — no API call, just JWT decode."""

    def test_aio_token_not_expired(self):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)
        token = os.getenv("AIO_TOKEN", "")
        if not token:
            print("SKIP: AIO_TOKEN not set in .env")
            return

        exp = _decode_jwt_exp(token)
        if exp is None:
            print("WARN: Could not decode AIO_TOKEN expiry")
            return

        now = int(time.time())
        remaining_minutes = (exp - now) // 60
        if exp < now:
            print(f"\nFAIL: AIO_TOKEN EXPIRED {abs(remaining_minutes)} minutes ago!")
            print("  Fix: refresh tokens from browser DevTools and paste into .env or admin UI")
            assert False, f"AIO_TOKEN expired {abs(remaining_minutes)} minutes ago"
        else:
            print(f"\nOK: AIO_TOKEN valid for {remaining_minutes} more minutes")


# ── 4. .env AI key state ──────────────────────────────────────────────────────

class TestEnvAiKeyState:
    """Shows which AI provider the current .env will select."""

    def test_show_ai_provider_selection(self):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)

        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        gemini_key = os.getenv("GEMINI_API_KEY", "")

        has_anthropic = bool(anthropic_key) and anthropic_key.startswith("sk-ant-")
        has_gemini = bool(gemini_key) and not gemini_key.startswith("<")

        print(f"\nANTHROPIC_API_KEY set: {bool(anthropic_key)}, valid format: {has_anthropic}")
        print(f"GEMINI_API_KEY    set: {bool(gemini_key)}, valid format: {has_gemini}")

        if has_anthropic:
            print("=> Will use: Claude (Anthropic)")
        elif has_gemini:
            print("=> Will use: Gemini (Google)")
        else:
            print("=> PROBLEM: No valid AI provider configured!")

        assert has_anthropic or has_gemini, (
            "No valid AI key found. "
            "Set ANTHROPIC_API_KEY (sk-ant-...) or GEMINI_API_KEY in .env"
        )
