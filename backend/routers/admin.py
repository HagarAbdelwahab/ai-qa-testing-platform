"""
Admin endpoints — /api/admin
Token refresh without server restart.
Tokens are stored in SQLite and override .env values at runtime.
"""
import os
from fastapi import APIRouter
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel

import db

router = APIRouter()

FRONTEND = Path(__file__).parent.parent.parent / "frontend"


class TokenUpdate(BaseModel):
    aio_token: str       # full value including "Bearer "
    aio_csrf: str
    aio_session: str


@router.get("/page")
def admin_page():
    return FileResponse(str(FRONTEND / "admin.html"))


@router.post("/tokens")
def update_tokens(body: TokenUpdate):
    """Update AIO tokens at runtime — no server restart needed."""
    db.setting_set("AIO_TOKEN", body.aio_token)
    db.setting_set("AIO_CSRF", body.aio_csrf)
    db.setting_set("AIO_SESSION", body.aio_session)

    # Apply immediately to the running process
    os.environ["AIO_TOKEN"] = body.aio_token
    os.environ["AIO_CSRF"] = body.aio_csrf
    os.environ["AIO_SESSION"] = body.aio_session

    # Patch the live aio_client module so next API call uses new tokens
    try:
        from agent import aio_client
        aio_client._token   = body.aio_token
        aio_client._csrf    = body.aio_csrf
        aio_client._session = body.aio_session
    except Exception:
        pass

    return {"status": "updated"}


@router.post("/tokens/refresh")
def trigger_token_refresh():
    """Trigger an immediate AIO token refresh via Playwright."""
    try:
        from agent import token_refresher
        success = token_refresher.refresh_tokens()
        return {"success": success}
    except ImportError:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"error": "playwright not installed"})
    except Exception as exc:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"error": str(exc)})


@router.get("/tokens/status")
def token_status():
    """Return which tokens are set (values redacted)."""
    return {
        "AIO_TOKEN":   bool(os.getenv("AIO_TOKEN")),
        "AIO_CSRF":    bool(os.getenv("AIO_CSRF")),
        "AIO_SESSION": bool(os.getenv("AIO_SESSION")),
        "db_override": db.setting_get("AIO_TOKEN") != "",
    }
