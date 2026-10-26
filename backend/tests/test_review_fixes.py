"""
Regression tests for the code-review fixes:
  1. Cancelled jobs keep their status; agent code can stop between steps.
  2. aio_client._refresh_and_retry propagates the retried call's own error.
  3. cmd_execute runs Playwright once and never analyses a stale results file.

Run with:
  cd testing-platform/backend && python -m pytest tests/test_review_fixes.py -v
"""
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))

import jobs  # noqa: E402


# ── 1. Cancellation ───────────────────────────────────────────────────────────

class TestCancellation:
    def test_complete_does_not_overwrite_cancelled(self):
        jid = jobs.create_job("t")
        assert jobs.cancel_job(jid)
        jobs.complete_job(jid, {"x": 1})
        job = jobs.get_job(jid)
        assert job["status"] == "cancelled"
        assert job["result"] is None

    def test_fail_does_not_overwrite_cancelled(self):
        jid = jobs.create_job("t")
        jobs.cancel_job(jid)
        jobs.fail_job(jid, "boom")
        job = jobs.get_job(jid)
        assert job["status"] == "cancelled"
        assert job["error"] is None

    def test_complete_and_fail_still_work_for_running_jobs(self):
        a, b = jobs.create_job("t"), jobs.create_job("t")
        jobs.complete_job(a, 1)
        jobs.fail_job(b, "e")
        assert jobs.get_job(a)["status"] == "completed"
        assert jobs.get_job(b)["status"] == "failed"

    def test_raise_if_cancelled_uses_the_bound_job(self):
        jid = jobs.create_job("t")
        with jobs.JobCapture(jid):
            jobs.raise_if_cancelled("step")      # running: no-op
            jobs.cancel_job(jid)
            with pytest.raises(jobs.JobCancelled):
                jobs.raise_if_cancelled("step")
        jobs.raise_if_cancelled("step")          # unbound thread: no-op


# ── 2. AIO refresh-and-retry ──────────────────────────────────────────────────

class TestRefreshAndRetry:
    def _fake_refresher(self, result):
        mod = types.ModuleType("token_refresher")
        mod.refresh_tokens = (lambda: result) if not isinstance(result, Exception) \
            else MagicMock(side_effect=result)
        return patch.dict(sys.modules, {"token_refresher": mod})

    def test_retried_call_error_propagates_unchanged(self):
        import aio_client
        resp = MagicMock(status_code=400)
        original = requests.HTTPError("400 Bad Request: field X invalid", response=resp)
        with self._fake_refresher(True):
            with pytest.raises(requests.HTTPError) as info:
                aio_client._refresh_and_retry(MagicMock(side_effect=original))
        assert info.value is original
        assert info.value.response is resp

    def test_returns_retry_result_when_refresh_works(self):
        import aio_client
        with self._fake_refresher(True):
            assert aio_client._refresh_and_retry(lambda: {"ok": 1}) == {"ok": 1}

    def test_failed_refresh_keeps_first_response(self):
        import aio_client
        resp = MagicMock(status_code=401)
        fn = MagicMock()
        with self._fake_refresher(False):
            with pytest.raises(requests.HTTPError) as info:
                aio_client._refresh_and_retry(fn, resp)
        assert info.value.response is resp
        fn.assert_not_called()


# ── 3. cmd_execute: one Playwright run, fresh results only ───────────────────

class TestExecutePlaywright:
    STATE = {
        "aio_tc_keys": ["TAT-T1"],
        "aio_tc_ids": [1],
        "test_cases": [{"title": "t"}],
        "aio_tc_key_map": {"t": "TAT-T1"},
        "subtasks": {},
    }

    def _run(self, tmp_path, payload=None, returncode=1):
        """Run cmd_execute with a fake Playwright that writes `payload` (if any)
        to the file named by PLAYWRIGHT_JSON_OUTPUT_FILE."""
        import qa_agent as qa
        results = tmp_path / "test-results" / "results.json"
        results.parent.mkdir()
        results.write_text('{"stale": true}')   # left over from an earlier run

        def fake_run(cmd, cwd, env, timeout):
            assert not results.exists(), "stale results must be deleted before the run"
            if payload is not None:
                Path(env["PLAYWRIGHT_JSON_OUTPUT_FILE"]).write_text(json.dumps(payload))
            return returncode

        with patch.object(qa.config, "validate_all"), \
             patch.object(qa.config, "PLAYWRIGHT_REPO_PATH", str(tmp_path)), \
             patch.object(qa, "_load_state", return_value=dict(self.STATE)), \
             patch.object(qa, "_run_playwright", side_effect=fake_run) as run, \
             patch.object(qa.bug_reporter, "report_failures_from_playwright_results",
                          return_value=[]) as report, \
             patch.object(qa.bug_reporter, "mark_passed_in_aio") as mark:
            qa.cmd_execute("TAT-1", None, "cycle")
        return run, report, mark, results

    def test_no_fresh_results_skips_reporting_and_drops_stale_file(self, tmp_path, capsys):
        run, report, mark, results = self._run(tmp_path, payload=None)
        assert run.call_count == 1
        report.assert_not_called()
        mark.assert_not_called()
        assert not results.exists()
        assert "skipping bug reporting" in capsys.readouterr().out

    def test_fresh_results_are_reported_once(self, tmp_path, capsys):
        payload = {"stats": {"expected": 1, "unexpected": 0}, "suites": []}
        run, report, mark, results = self._run(tmp_path, payload=payload, returncode=0)
        assert run.call_count == 1
        cmd, _cwd, env, timeout = run.call_args.args
        assert "--reporter=list,json" in cmd
        assert env["PLAYWRIGHT_JSON_OUTPUT_FILE"] == str(results)
        assert timeout > 0
        assert json.loads(results.read_text()) == payload
        report.assert_called_once()
        mark.assert_called_once()
        assert "1 passed, 0 failed" in capsys.readouterr().out

    def test_timeout_becomes_runtime_error(self, tmp_path):
        import subprocess
        import qa_agent as qa
        with patch.object(qa.config, "validate_all"), \
             patch.object(qa.config, "PLAYWRIGHT_REPO_PATH", str(tmp_path)), \
             patch.object(qa, "_load_state", return_value=dict(self.STATE)), \
             patch.object(qa, "_run_playwright",
                          side_effect=subprocess.TimeoutExpired("npx", 5)), \
             patch.object(qa.bug_reporter, "report_failures_from_playwright_results") as report:
            with pytest.raises(RuntimeError, match="exceeded"):
                qa.cmd_execute("TAT-1", None, "cycle")
        report.assert_not_called()

    def test_cancelled_job_stops_before_playwright(self, tmp_path):
        import qa_agent as qa
        jid = jobs.create_job("execute")
        jobs.cancel_job(jid)
        with jobs.JobCapture(jid), \
             patch.object(qa.config, "validate_all"), \
             patch.object(qa.config, "PLAYWRIGHT_REPO_PATH", str(tmp_path)), \
             patch.object(qa, "_load_state", return_value=dict(self.STATE)), \
             patch.object(qa, "_run_playwright") as run:
            with pytest.raises(jobs.JobCancelled):
                qa.cmd_execute("TAT-1", None, "cycle")
        run.assert_not_called()


# ── 4. _run_playwright: process group + live output ──────────────────────────

class TestRunPlaywright:
    def test_timeout_kills_the_whole_process_group(self):
        import subprocess
        import qa_agent as qa
        proc = MagicMock(pid=4242, stdout=iter([]))
        proc.wait.return_value = 0   # exits once killed; deadline is already past
        with patch.object(qa.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(qa.os, "name", "posix"), \
             patch.object(qa.os, "killpg") as killpg:
            with pytest.raises(subprocess.TimeoutExpired):
                qa._run_playwright(["npx", "playwright", "test"], "/tmp", {}, timeout=0)
        assert popen.call_args.kwargs["start_new_session"] is True
        killpg.assert_called_once_with(4242, qa.signal.SIGKILL)

    def test_group_already_gone_is_not_an_error(self):
        import subprocess
        import qa_agent as qa
        proc = MagicMock(pid=4242, stdout=iter([]))
        proc.wait.return_value = 0   # exits once killed; deadline is already past
        with patch.object(qa.subprocess, "Popen", return_value=proc), \
             patch.object(qa.os, "name", "posix"), \
             patch.object(qa.os, "killpg", side_effect=ProcessLookupError):
            with pytest.raises(subprocess.TimeoutExpired):
                qa._run_playwright(["npx"], "/tmp", {}, timeout=0)

    def test_keyboard_interrupt_kills_the_group(self):
        import qa_agent as qa
        proc = MagicMock(pid=4242, stdout=iter([]))
        proc.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch.object(qa.subprocess, "Popen", return_value=proc), \
             patch.object(qa.os, "name", "posix"), \
             patch.object(qa.os, "killpg") as killpg:
            with pytest.raises(KeyboardInterrupt):
                qa._run_playwright(["npx"], "/tmp", {}, timeout=60)
        killpg.assert_called_once_with(4242, qa.signal.SIGKILL)

    def test_cancel_during_run_kills_the_group(self):
        import subprocess
        import qa_agent as qa
        proc = MagicMock(pid=4242, stdout=iter([]))
        # Still running on the first two polls, then exits after the kill.
        proc.wait.side_effect = [subprocess.TimeoutExpired("npx", 5)] * 2 + [0]
        flags = iter([False, True])
        with patch.object(qa.subprocess, "Popen", return_value=proc), \
             patch.object(qa.os, "name", "posix"), \
             patch.object(qa.os, "killpg") as killpg, \
             patch.object(qa, "_current_job_cancelled", side_effect=lambda: next(flags)):
            with pytest.raises(qa.JobCancelled):
                qa._run_playwright(["npx"], "/tmp", {}, timeout=3600)
        killpg.assert_called_once_with(4242, qa.signal.SIGKILL)
        assert proc.wait.call_args_list[0].kwargs["timeout"] == qa.CANCEL_POLL_SECONDS

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
    def test_streams_output_into_the_job_log(self):
        import os
        import qa_agent as qa
        jid = jobs.create_job("execute")
        with jobs.JobCapture(jid):
            rc = qa._run_playwright(["sh", "-c", "echo line-one; echo line-two >&2; exit 3"],
                                    "/tmp", dict(os.environ), timeout=30)
        assert rc == 3
        logs = jobs.get_job(jid)["logs"]
        assert "line-one" in logs and "line-two" in logs

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    def test_real_timeout_kills_grandchildren(self, tmp_path):
        import os
        import subprocess
        import time
        import qa_agent as qa
        pidfile = tmp_path / "child.pid"
        # The shell starts a background grandchild, records its pid, then hangs.
        script = f"sleep 60 & echo $! > {pidfile}; wait"
        with pytest.raises(subprocess.TimeoutExpired):
            qa._run_playwright(["sh", "-c", script], str(tmp_path), dict(os.environ), timeout=1)
        grandchild = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("grandchild survived the timeout")


# ── 5. Claude CLI login check ─────────────────────────────────────────────────

class TestClaudeCliAvailable:
    def _check(self, which="/usr/bin/claude", stdout=None, exc=None):
        import config
        run = MagicMock(side_effect=exc) if exc else \
            MagicMock(return_value=MagicMock(returncode=0, stdout=stdout or ""))
        with patch("shutil.which", return_value=which), patch("subprocess.run", run):
            return config.claude_cli_available(), run

    def test_not_on_path(self):
        ok, run = self._check(which=None)
        assert ok is False
        run.assert_not_called()

    def test_logged_in(self):
        ok, run = self._check(stdout='{"loggedIn": true}')
        assert ok is True
        assert run.call_args.args[0][1:] == ["auth", "status", "--json"]

    def test_logged_out(self):
        assert self._check(stdout='{"loggedIn": false}')[0] is False

    def test_older_cli_without_auth_status_falls_back_to_path(self):
        assert self._check(stdout="error: unknown command 'auth'")[0] is True

    def test_hung_status_falls_back_to_path(self):
        import subprocess
        assert self._check(exc=subprocess.TimeoutExpired("claude", 20))[0] is True
