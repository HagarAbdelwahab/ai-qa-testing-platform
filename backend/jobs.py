"""In-memory job store + stdout capture for background agent tasks."""
import contextvars
import sys
import threading
import uuid
from typing import Optional

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def create_job(job_type: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": job_type,
            "status": "running",
            "logs": [],
            "result": None,
            "error": None,
        }
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    return _jobs.get(job_id)


def add_log(job_id: str, message: str) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["logs"].append(message)


def complete_job(job_id: str, result=None) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["result"] = result


def fail_job(job_id: str, error: str) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = error


def cancel_job(job_id: str) -> bool:
    """Mark a running job as cancelled. Returns True if it was running."""
    with _lock:
        job = _jobs.get(job_id)
        if job and job["status"] == "running":
            job["status"] = "cancelled"
            return True
        return False


def is_cancelled(job_id: str) -> bool:
    job = _jobs.get(job_id)
    return job is not None and job["status"] == "cancelled"


# ── stdout/stderr routing ─────────────────────────────────────────────────────
#
# Jobs run in their own threads but sys.stdout is process-global, so a capture
# that swaps sys.stdout leaks one job's output into another's log and silences
# whichever job loses the race. Instead we install ONE router on sys.stdout for
# the life of the process; it looks up the job bound to the *calling* thread and
# appends there. Unbound threads (uvicorn, the token refresher) print to the
# terminal only, exactly as before.

_current_job: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_job_id", default=None
)


class _StdoutRouter:
    """Tees a stream to the terminal and to the calling thread's job log."""

    _is_job_router = True

    def __init__(self, real_stream):
        self._real = real_stream
        self._bufs: dict[str, str] = {}   # job_id -> partial line
        self._buf_lock = threading.Lock()

    # -- stream protocol --
    def write(self, text: str) -> int:
        written = self._real.write(text)
        job_id = _current_job.get()
        if job_id:
            self._append(job_id, text)
        return written

    def flush(self) -> None:
        self._real.flush()

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        # encoding, fileno, isatty, buffer … anything we don't override
        return getattr(self._real, name)

    # -- job log plumbing --
    def _append(self, job_id: str, text: str) -> None:
        with self._buf_lock:
            buf = self._bufs.get(job_id, "") + text
            lines = buf.split("\n")
            self._bufs[job_id] = lines.pop()   # trailing partial line
        for line in lines:
            if line.strip():
                add_log(job_id, line)

    def release(self, job_id: str) -> None:
        """Flush any trailing partial line and drop the job's buffer."""
        with self._buf_lock:
            tail = self._bufs.pop(job_id, "")
        if tail.strip():
            add_log(job_id, tail)


def _install_router(attr: str) -> "_StdoutRouter":
    """Idempotently wrap sys.stdout / sys.stderr in a router and return it."""
    stream = getattr(sys, attr)
    if getattr(stream, "_is_job_router", False):
        return stream
    router = _StdoutRouter(stream)
    setattr(sys, attr, router)
    return router


class JobCapture:
    """
    Context manager that tees the calling thread's stdout/stderr into the job
    log while still printing to the real streams, so server logs stay visible.

    Safe under concurrency: the binding is per-thread, so overlapping jobs each
    get only their own output.
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._token = None
        self._routers: list[_StdoutRouter] = []

    def __enter__(self):
        self._routers = [_install_router("stdout"), _install_router("stderr")]
        self._token = _current_job.set(self.job_id)
        return self

    def __exit__(self, *_):
        _current_job.reset(self._token)
        for router in self._routers:
            router.release(self.job_id)
