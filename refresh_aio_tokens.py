"""
AIO Token Refresher — uses Playwright to open Jira in a browser,
navigate to the AIO app, intercept a real network request, capture
the fresh Forge JWT headers, and write them directly to .env.

Usage:
    python3 refresh_aio_tokens.py

Requirements:
    pip install playwright python-dotenv
    playwright install chromium

The script opens a VISIBLE browser so you can log in to Jira if needed.
Once Jira is open and the AIO app loads, tokens are captured automatically.
"""

import asyncio
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"

load_dotenv(ENV_PATH)
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "")
AIO_DOMAIN    = "tcms.aiojiraapps.com"
# Navigate to a recent Jira issue — AIO issue panel loads automatically and triggers tokens
AIO_JIRA_PAGE = os.getenv("AIO_PAGE_URL", "")


def _update_env(key: str, value: str):
    """Replace or append KEY=VALUE in .env (preserves all other lines)."""
    text = ENV_PATH.read_text()
    pattern = rf"^{re.escape(key)}=.*$"
    replacement = f"{key}={value}"
    if re.search(pattern, text, flags=re.MULTILINE):
        text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
    else:
        text += f"\n{replacement}\n"
    ENV_PATH.write_text(text)


USER_DATA_DIR = str(Path(__file__).parent / ".playwright-session")


async def capture_tokens():
    from playwright.async_api import async_playwright

    captured = {}

    async with async_playwright() as pw:
        # Persistent context so Jira login is saved between runs
        context = await pw.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print(f"Opening: {AIO_JIRA_PAGE}")
        print("Log in to Jira if prompted. Waiting for AIO panel to load...")
        print("(login is saved after first run)\n")

        # High-level context listener — captures requests from all pages + frames
        def handle_request(request):
            if captured:
                return
            url = request.url
            if AIO_DOMAIN in url:
                h = request.headers  # already lowercase keys in Playwright
                token   = h.get("authorization", "")
                csrf    = h.get("aio-tests-csrf-token", "")
                session = h.get("x-aio-tests-forge-session", "")
                print(f"  [AIO] {url[:80]}")
                print(f"        auth={bool(token)} csrf={bool(csrf)} session={bool(session)}")
                if token.startswith("Bearer ") and csrf and session:
                    captured["AIO_TOKEN"]   = token
                    captured["AIO_CSRF"]    = csrf
                    captured["AIO_SESSION"] = session
                    print(f"\nTokens captured!")
                    print(f"  AIO_CSRF:    {csrf}")
                    print(f"  AIO_SESSION: {session}")
                    print(f"  AIO_TOKEN:   {token[:60]}...")

        context.on("request", handle_request)

        await page.goto(AIO_JIRA_PAGE, wait_until="domcontentloaded", timeout=60_000)

        # Wait up to 120 seconds — report page URL every 20s to help diagnose
        for i in range(120):
            if captured:
                break
            if i % 20 == 0 and i > 0:
                current_url = page.url
                if "login" in current_url or "id.atlassian" in current_url:
                    print(f"  [{i}s] Waiting for login... complete sign-in in the browser window")
                else:
                    print(f"  [{i}s] On: {current_url[:80]}")
                    print(f"       Waiting for AIO panel — scroll to the right side panel if needed")
            await asyncio.sleep(1)

        await context.close()

    if not captured:
        print("\nNo AIO tokens captured. Make sure the AIO panel loaded in the browser.")
        sys.exit(1)

    return captured


def main():
    load_dotenv(ENV_PATH)

    print("=== AIO Token Refresher ===")
    tokens = asyncio.run(capture_tokens())

    print("\nWriting tokens to .env ...")
    for key, value in tokens.items():
        _update_env(key, value)
        os.environ[key] = value

    print("Done! Tokens updated in .env")
    print("The running server will pick them up via /api/admin/tokens or on next restart.")

    # If the testing-platform server is running, push tokens live without restart
    try:
        import requests as req
        r = req.post(
            "http://localhost:8000/api/admin/tokens",
            json={
                "aio_token":   tokens["AIO_TOKEN"],
                "aio_csrf":    tokens["AIO_CSRF"],
                "aio_session": tokens["AIO_SESSION"],
            },
            timeout=5,
        )
        if r.ok:
            print("Live server updated — no restart needed.")
        else:
            print(f"Server update returned {r.status_code} — restart the server to apply.")
    except Exception:
        print("(Server not reachable — tokens saved to .env, restart to apply.)")


if __name__ == "__main__":
    main()
