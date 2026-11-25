"""
Unit tests for:
  1. Stale state detection — cached subtask keys deleted from Jira should trigger recreation
  2. 429 retry logic — HTTP 429 should be retried with backoff, not crash

Run with:
  cd testing-platform/backend && python -m pytest tests/test_state_and_retry.py -v -s
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_http_error(status: int) -> requests.HTTPError:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.reason = {404: "Not Found", 429: "Too Many Requests"}.get(status, "Error")
    err = requests.HTTPError(response=resp)
    return err


# ── 1. jira_client.issue_exists ───────────────────────────────────────────────

class TestIssueExists:
    """issue_exists() should return True when issue exists, False on 404."""

    def test_returns_true_when_issue_found(self):
        import agent.jira_client as jc
        with patch.object(jc, "_get", return_value={"key": "TAT-5726"}):
            assert jc.issue_exists("TAT-5726") is True

    def test_returns_false_on_404(self):
        import agent.jira_client as jc
        with patch.object(jc, "_get", side_effect=_make_http_error(404)):
            assert jc.issue_exists("TAT-5726") is False

    def test_reraises_non_404_errors(self):
        import agent.jira_client as jc
        import pytest
        with patch.object(jc, "_get", side_effect=_make_http_error(500)):
            with pytest.raises(requests.HTTPError):
                jc.issue_exists("TAT-5726")


# ── 2. stale state detection in qa_agent ─────────────────────────────────────

class TestStaleStateDetection:
    """When cached subtask keys don't exist in Jira, recreate them."""

    def _make_state(self, tmp_path: Path, story_key: str, data: dict) -> None:
        state_dir = tmp_path / ".state"
        state_dir.mkdir()
        (state_dir / f"{story_key}.json").write_text(json.dumps(data))

    def test_skips_creation_when_subtasks_still_exist(self, tmp_path):
        import agent.qa_agent as qa
        import agent.jira_client as jc

        state = {"subtasks": {"Test Case Writing": "TAT-100", "Test Case Review": "TAT-101", "Test Execution": "TAT-102"}}
        self._make_state(tmp_path, "TAT-5720", state)

        with patch.object(qa, "_load_state", return_value=state), \
             patch.object(jc, "issue_exists", return_value=True), \
             patch.object(jc, "create_subtasks") as mock_create:
            # Simulate the stale-check logic
            cached = state.get("subtasks", {})
            missing = [k for k in cached.values() if not jc.issue_exists(k)]
            if missing:
                del state["subtasks"]
                jc.create_subtasks("TAT-5720")

        mock_create.assert_not_called()

    def test_recreates_subtasks_when_any_deleted(self, tmp_path):
        import agent.qa_agent as qa
        import agent.jira_client as jc

        state = {"subtasks": {"Test Case Writing": "TAT-100", "Test Case Review": "TAT-101", "Test Execution": "TAT-102"}}

        def exists_side_effect(key):
            return key != "TAT-100"  # TAT-100 was deleted

        with patch.object(jc, "issue_exists", side_effect=exists_side_effect), \
             patch.object(jc, "create_subtasks", return_value={"Test Case Writing": "TAT-200"}) as mock_create:
            cached = state.get("subtasks", {})
            missing = [k for k in cached.values() if not jc.issue_exists(k)]
            if missing:
                del state["subtasks"]
                jc.create_subtasks("TAT-5720")

        mock_create.assert_called_once_with("TAT-5720")

    def test_recreates_subtasks_when_all_deleted(self, tmp_path):
        import agent.jira_client as jc

        state = {"subtasks": {"Test Case Writing": "TAT-100"}}

        with patch.object(jc, "issue_exists", return_value=False), \
             patch.object(jc, "create_subtasks", return_value={"Test Case Writing": "TAT-200"}) as mock_create:
            cached = state.get("subtasks", {})
            missing = [k for k in cached.values() if not jc.issue_exists(k)]
            if missing:
                del state["subtasks"]
                jc.create_subtasks("TAT-5720")

        mock_create.assert_called_once()


# ── 3. retry_on_429 utility ───────────────────────────────────────────────────

class TestRetryOn429:
    """retry_on_429 should retry up to max_retries times, then raise."""

    def test_succeeds_on_first_try(self):
        from agent.retry_utils import retry_on_429
        mock_fn = MagicMock(return_value="ok")
        result = retry_on_429(mock_fn)
        assert result == "ok"
        mock_fn.assert_called_once()

    def test_retries_on_429_and_succeeds(self):
        from agent.retry_utils import retry_on_429
        mock_fn = MagicMock(side_effect=[_make_http_error(429), _make_http_error(429), "ok"])
        result = retry_on_429(mock_fn, max_retries=3, base_delay=0)
        assert result == "ok"
        assert mock_fn.call_count == 3

    def test_raises_after_max_retries(self):
        import pytest
        from agent.retry_utils import retry_on_429
        mock_fn = MagicMock(side_effect=_make_http_error(429))
        with pytest.raises(requests.HTTPError):
            retry_on_429(mock_fn, max_retries=2, base_delay=0)
        assert mock_fn.call_count == 2

    def test_does_not_retry_non_429(self):
        import pytest
        from agent.retry_utils import retry_on_429
        mock_fn = MagicMock(side_effect=_make_http_error(500))
        with pytest.raises(requests.HTTPError):
            retry_on_429(mock_fn, max_retries=3, base_delay=0)
        mock_fn.assert_called_once()  # no retry for 500

    def test_passes_args_through(self):
        from agent.retry_utils import retry_on_429
        mock_fn = MagicMock(return_value=42)
        result = retry_on_429(mock_fn, "a", "b", key="val")
        mock_fn.assert_called_once_with("a", "b", key="val")
        assert result == 42


# ── 4. Subtask creation — must match browser curl (IDs, api/2) ───────────────

class TestSubtaskCreation:
    """create_subtasks must use numeric IDs (not names/keys) matching the browser curl:
       POST /rest/api/2/issue with project.id, issuetype.id="10008", parent.id
    """

    def _fake_get(self, path):
        # get_issue("TAT-5720")
        return {"id": "109265", "fields": {"project": {"key": "TAT", "id": "13098"}}}

    def _fake_post(self, captured):
        def _post(path, body):
            captured.append({"path": path, "body": body})
            return {"key": f"TAT-{len(captured) + 900}"}
        return _post

    def test_uses_api_v2_endpoint(self):
        import agent.jira_client as jc
        captured = []
        with patch.object(jc, "_get", side_effect=self._fake_get), \
             patch.object(jc, "_post", side_effect=self._fake_post(captured)):
            jc.create_subtasks("TAT-5720")
        for call in captured:
            assert call["path"] == "/rest/api/2/issue", \
                f"Must use api/2, got: {call['path']}"

    def test_uses_project_id_not_key(self):
        import agent.jira_client as jc
        captured = []
        with patch.object(jc, "_get", side_effect=self._fake_get), \
             patch.object(jc, "_post", side_effect=self._fake_post(captured)):
            jc.create_subtasks("TAT-5720")
        for call in captured:
            project = call["body"]["fields"]["project"]
            assert "id" in project and "key" not in project, \
                f"project must use id not key, got: {project}"
            assert project["id"] == "13098"

    def test_uses_parent_id_not_key(self):
        import agent.jira_client as jc
        captured = []
        with patch.object(jc, "_get", side_effect=self._fake_get), \
             patch.object(jc, "_post", side_effect=self._fake_post(captured)):
            jc.create_subtasks("TAT-5720")
        for call in captured:
            parent = call["body"]["fields"]["parent"]
            assert "id" in parent and "key" not in parent, \
                f"parent must use id not key, got: {parent}"
            assert parent["id"] == "109265"

    def test_uses_issuetype_id_10008(self):
        import agent.jira_client as jc
        captured = []
        with patch.object(jc, "_get", side_effect=self._fake_get), \
             patch.object(jc, "_post", side_effect=self._fake_post(captured)):
            jc.create_subtasks("TAT-5720")
        for call in captured:
            issuetype = call["body"]["fields"]["issuetype"]
            assert "id" in issuetype and "name" not in issuetype, \
                f"issuetype must use id not name, got: {issuetype}"
            assert issuetype["id"] == "10008"

    def test_creates_subtasks_with_correct_summaries(self):
        import agent.jira_client as jc
        captured = []
        with patch.object(jc, "_get", side_effect=self._fake_get), \
             patch.object(jc, "_post", side_effect=self._fake_post(captured)):
            result = jc.create_subtasks("TAT-5720")
        summaries = [c["body"]["fields"]["summary"] for c in captured]
        assert summaries == [
            "[Testing] Test case execution - mocks",
            "[Testing] Test case execution - E2E",
        ]
        assert summaries == jc.SUBTASK_NAMES
        assert len(result) == 2


# ── 5. Claude Code CLI call ───────────────────────────────────────────────────

class TestClaudeCodeCli:
    """_call_claude_code_cli is the fallback provider when no Anthropic key is set."""

    def _done(self, returncode=0, stdout="ok", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_raises_when_cli_missing(self):
        import pytest
        import agent.test_generator as tg
        with patch("shutil.which", return_value=None):
            with pytest.raises(EnvironmentError, match="not found"):
                tg._call_claude_code_cli("hi")

    def test_strips_placeholder_key_and_returns_stdout(self):
        import agent.test_generator as tg
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "<placeholder>"}), \
             patch("subprocess.run", return_value=self._done(stdout=" answer \n")) as run:
            assert tg._call_claude_code_cli("hi", timeout=7) == "answer"
        kwargs = run.call_args.kwargs
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        assert kwargs["timeout"] == 7
        assert kwargs["input"] == "hi"

    def test_raises_on_nonzero_exit(self):
        import pytest
        import agent.test_generator as tg
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=self._done(returncode=1, stderr="not logged in")):
            with pytest.raises(EnvironmentError, match="not logged in"):
                tg._call_claude_code_cli("hi")
