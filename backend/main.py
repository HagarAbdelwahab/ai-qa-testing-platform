"""
QA Testing Platform — FastAPI backend
--------------------------------------
Serves the frontend and exposes four API groups:
  /api/jira      — Jira ticket proxy
  /api/testware  — Testware prep (kickoff + SSE stream)
  /api/execution — Test execution (run + SSE stream)
  /api/knowledge — Knowledge Base CRUD
"""
import os
import sys
from pathlib import Path

# Allow importing sibling modules without installing as a package
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db
from routers import admin, agents, execution, jira_proxy, knowledge, testware

app = FastAPI(title="QA Testing Platform", version="1.0.0")

# Same-origin requests (the bundled frontend) never need CORS. Only listed
# origins may call the API from another site; a wildcard would let any page
# open in the browser drive /api/admin/tokens or create AIO cases.
# Override with a comma-separated CORS_ORIGINS when deployed.
_cors_origins = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routers ───────────────────────────────────────────────────────────────
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(jira_proxy.router, prefix="/api/jira", tags=["jira"])
app.include_router(testware.router, prefix="/api/testware", tags=["testware"])
app.include_router(execution.router, prefix="/api/execution", tags=["execution"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["knowledge"])

# ── Static frontend ───────────────────────────────────────────────────────────
FRONTEND = Path(__file__).parent.parent / "frontend"
STATIC = FRONTEND / "static"

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(FRONTEND / "index.html"))


@app.get("/knowledge-base")
def knowledge_page():
    return FileResponse(str(FRONTEND / "knowledge.html"))


@app.get("/admin")
def admin_page():
    return FileResponse(str(FRONTEND / "admin.html"))


@app.get("/agents")
def agents_page():
    return FileResponse(str(FRONTEND / "agents.html"))


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup():
    db.init_db()
    print("  DB initialised at", db.DB_PATH)
