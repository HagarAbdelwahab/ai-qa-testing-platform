"""Agent endpoints — /api/agents"""
import asyncio
import json
import threading

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import bridge
import jobs

router = APIRouter()


# ── Request models ─────────────────────────────────────────────────────────────

class PullSprintRequest(BaseModel):
    sprint_name: str
    issue_types: str = "Story"


class ReviewStoryRequest(BaseModel):
    issue_key: str


class CreateSubtasksRequest(BaseModel):
    issue_keys: str          # comma-separated, e.g. "TAT-100,TAT-101"


class AioTestwareRequest(BaseModel):
    issue_key: str
    cycle_name: str = ""


class TestSummaryGatherRequest(BaseModel):
    cycle_name: str = ""
    issue_keys: str          # epic and/or story keys, comma-separated
    pull_aio: bool = True


class TestSummaryGenerateRequest(BaseModel):
    report: dict


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/pull-sprint")
def pull_sprint(req: PullSprintRequest):
    try:
        stories, sprint_name, cycle_name = bridge.pull_sprint_stories(req.sprint_name, req.issue_types)
        return {"stories": stories, "total": len(stories), "sprint_name": sprint_name, "cycle_name": cycle_name}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/cycle-stats")
def cycle_stats(cycle_name: str):
    try:
        return bridge.get_cycle_stats(cycle_name)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/review-story")
def review_story(req: ReviewStoryRequest):
    job_id = jobs.create_job("review-story")
    threading.Thread(
        target=bridge.run_review_story,
        args=(job_id, req.issue_key.strip().upper()),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@router.post("/create-subtasks")
def create_subtasks(req: CreateSubtasksRequest):
    keys = [k.strip().upper() for k in req.issue_keys.split(",") if k.strip()]
    if not keys:
        return JSONResponse(status_code=400, content={"error": "No issue keys provided"})
    job_id = jobs.create_job("create-subtasks")
    threading.Thread(
        target=bridge.run_create_subtasks,
        args=(job_id, keys),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@router.post("/aio-testware")
def aio_testware(req: AioTestwareRequest):
    job_id = jobs.create_job("aio-testware")
    threading.Thread(
        target=bridge.run_kickoff,
        args=(job_id, [req.issue_key.strip().upper()], req.cycle_name.strip(), ""),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@router.post("/test-summary/gather")
def test_summary_gather(req: TestSummaryGatherRequest):
    keys = [k.strip().upper() for k in req.issue_keys.split(",") if k.strip()]
    if not keys:
        return JSONResponse(status_code=400, content={"error": "No epic/story keys provided"})
    job_id = jobs.create_job("test-summary-gather")
    threading.Thread(
        target=bridge.run_test_summary_gather,
        args=(job_id, req.cycle_name.strip(), keys, req.pull_aio),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@router.post("/test-summary/generate")
def test_summary_generate(req: TestSummaryGenerateRequest):
    job_id = jobs.create_job("test-summary-generate")
    threading.Thread(
        target=bridge.run_test_summary_generate,
        args=(job_id, req.report),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@router.get("/test-summary/download/{filename}")
def test_summary_download(filename: str):
    from agent.test_summary_report import REPORTS_DIR
    path = (REPORTS_DIR / filename).resolve()
    if path.parent != REPORTS_DIR.resolve() or not path.is_file() or path.suffix not in (".docx", ".pdf"):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    return FileResponse(str(path), filename=filename)


@router.post("/stop/{job_id}")
def stop_job(job_id: str):
    """Cancel a running job."""
    cancelled = jobs.cancel_job(job_id)
    return {"cancelled": cancelled}


@router.get("/stream/{job_id}")
async def stream(job_id: str):
    """SSE — streams job logs and final result."""

    async def generator():
        sent = 0
        while True:
            job = jobs.get_job(job_id)
            if not job:
                yield {"data": json.dumps({"type": "error", "message": "Job not found"})}
                break

            for line in job["logs"][sent:]:
                yield {"data": json.dumps({"type": "log", "message": line})}
                sent += 1

            if job["status"] == "completed":
                yield {"data": json.dumps({
                    "type": "done",
                    "status": "completed",
                    "result": job["result"],
                })}
                break
            elif job["status"] == "failed":
                yield {"data": json.dumps({
                    "type": "done",
                    "status": "failed",
                    "error": job["error"],
                })}
                break
            elif job["status"] == "cancelled":
                yield {"data": json.dumps({
                    "type": "done",
                    "status": "cancelled",
                })}
                break

            await asyncio.sleep(0.35)

    return EventSourceResponse(generator())
