"""Test execution — /api/execution"""
import asyncio
import json
import threading

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import bridge
import jobs

router = APIRouter()


class ExecuteRequest(BaseModel):
    issue: str
    cycle_name: str
    data_file: str | None = None


@router.post("/run")
def run(req: ExecuteRequest):
    job_id = jobs.create_job("execute")

    thread = threading.Thread(
        target=bridge.run_execute,
        args=(job_id, req.issue, req.cycle_name, req.data_file),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@router.get("/status/{job_id}")
def job_status(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return job


@router.get("/stream/{job_id}")
async def stream(job_id: str):
    """SSE endpoint — streams execution logs in real time."""

    async def generator():
        sent = 0
        while True:
            job = jobs.get_job(job_id)
            if not job:
                yield {"data": json.dumps({"type": "error", "message": "Job not found"})}
                break

            new_logs = job["logs"][sent:]
            for line in new_logs:
                yield {"data": json.dumps({"type": "log", "message": line})}
                sent += 1

            if job["status"] == "completed":
                yield {
                    "data": json.dumps(
                        {"type": "done", "status": "completed", "result": job["result"]}
                    )
                }
                break
            elif job["status"] == "failed":
                yield {
                    "data": json.dumps(
                        {"type": "done", "status": "failed", "error": job["error"]}
                    )
                }
                break

            await asyncio.sleep(0.35)

    return EventSourceResponse(generator())
