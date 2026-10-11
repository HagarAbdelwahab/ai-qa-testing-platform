"""
Automated AIO token refresh via Playwright.
Logs into Jira, navigates to a Story issue where the AIO TCMS panel loads,
intercepts requests to tcms.aiojiraapps.com, and stores fresh auth headers
in the DB so all AIO calls pick them up automatically.

Required .env vars:
  JIRA_EMAIL      — Jira account email
  JIRA_PASSWORD   — Jira account password

Optional:
  AIO_PAGE_URL    — URL of a Jira Story with the AIO panel (auto-discovered if missing or stale)

Two-pass strategy
-----------------
Atlassian's reCAPTCHA fires against headless Chromium, blocking the email form
submission.  We try headless first (silent, server-safe).  If login is blocked
we retry with headless=False — a real browser window bypasses reCAPTCHA.  After
one successful login the session state is saved and all subsequent 40-min
refreshes run headless without any login at all.
"""
import asyncio
import os
import threading
import time
from pathlib import Path

_STATE_FILE = Path(__file__).parent.parent / ".aio_browser_state.json"
_REFRESH_INTERVAL = 40 * 60  # 40 minutes — AIO tokens expire in 45 min
_lock = threading.Lock()


class _LoginBlockedError(Exception):
    """Raised when headless login is blocked (e.g. reCAPTCHA)."""


async def _do_login(page, config) -> None:
    """Handle Atlassian login + company SSO (Microsoft Azure AD) redirect."""
    # Step 1: email — Atlassian shows email first; #password appears only after submit
    try:
        await page.locator("#username").wait_for(state="visible", timeout=10_000)
        await page.locator("#username").fill(config.JIRA_EMAIL)
        await page.locator("#login-submit").click()
        await page.wait_for_timeout(5_000)  # animated transition to password step
    except Exception:
        pass  # already past email step, or SSO jumped straight to provider

    current_url = page.url
    if "login.microsoftonline.com" in current_url or "microsoftonline.com" in current_url:
        await page.locator("[name=passwd]").fill(config.JIRA_PASSWORD, timeout=15_000)
        await page.get_by_role("button", name="Sign in").click()
        try:
            await page.get_by_role("button", name="Yes").click(timeout=5_000)
        except Exception:
            pass
        await page.wait_for_load_state("networkidle", timeout=30_000)

    elif "id.atlassian.com" in current_url:
        # #password is in the DOM from the start but hidden until email is submitted.
        # If still hidden after the 5 s wait, the email click was blocked — try once more.
        try:
            await page.locator("#password").wait_for(state="visible", timeout=8_000)
        except Exception:
            try:
                await page.locator("#login-submit").click()
                await page.wait_for_timeout(3_000)
            except Exception:
                pass
            await page.locator("#password").wait_for(state="visible", timeout=15_000)

        await page.locator("#password").fill(config.JIRA_PASSWORD)
        await page.locator("#login-submit").click()
        await page.wait_for_load_state("networkidle", timeout=30_000)

        # Atlassian may hand off to Microsoft SSO after the Atlassian password step
        if "microsoftonline.com" in page.url:
            await page.locator("[name=passwd]").fill(config.JIRA_PASSWORD, timeout=15_000)
            await page.get_by_role("button", name="Sign in").click()
            try:
                await page.get_by_role("button", name="Yes").click(timeout=5_000)
            except Exception:
                pass
            await page.wait_for_load_state("networkidle", timeout=30_000)

    else:
        raise RuntimeError(f"unrecognised login page: {current_url}")


async def _find_story_urls(config, n: int = 3) -> list:
    """Query Jira REST API to find recent Story URLs for AIO token capture."""
    import base64
    import requests

    def _query():
        creds = base64.b64encode(
            f"{config.JIRA_EMAIL}:{config.JIRA_API_TOKEN}".encode()
        ).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        jql = (
            f"project = {config.JIRA_PROJECT_KEY} AND issuetype = Story "
            f"ORDER BY updated DESC"
        )
        resp = requests.post(
            f"{config.JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=headers,
            json={"jql": jql, "maxResults": n, "fields": ["summary"]},
            timeout=10,
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        if not issues:
            raise RuntimeError("No Stories found in Jira project")
        return [f"{config.JIRA_BASE_URL}/browse/{i['key']}" for i in issues]

    return await asyncio.to_thread(_query)


def _is_login_page(url: str) -> bool:
    return (
        "atlassian.com/login" in url
        or "id.atlassian.com" in url
        or "/join/" in url
    )


async def _capture_async(headless: bool = True) -> dict:
    import config as _config
    from playwright.async_api import async_playwright

    # Build candidate list: configured/cached URL first, then auto-discovered Stories
    candidates: list = []
    configured = _config._get("AIO_PAGE_URL")
    if configured:
        candidates.append(configured)

    if _config.JIRA_API_TOKEN:
        try:
            discovered = await _find_story_urls(_config, n=3)
            for url in discovered:
                if url not in candidates:
                    candidates.append(url)
            print(f"  AIO refresh: auto-discovered {len(discovered)} Story URL(s) from Jira")
        except Exception as exc:
            print(f"  AIO refresh: Jira Story discovery failed — {exc}")

    if not candidates:
        raise RuntimeError(
            "No Story URLs available — set AIO_PAGE_URL or ensure JIRA_API_TOKEN is configured"
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx_kwargs = {
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        }
        if _STATE_FILE.exists():
            ctx_kwargs["storage_state"] = str(_STATE_FILE)

        context = await browser.new_context(**ctx_kwargs)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        captured: dict = {}
        got_tokens = asyncio.Event()
        login_was_blocked = False
        logged_in = False
        seen_domains: set = set()

        async def on_request(request):
            if "tcms.aiojiraapps.com" not in request.url:
                return
            h = dict(request.headers)
            if h.get("authorization", "").startswith("Bearer "):
                captured["AIO_TOKEN"] = h["authorization"]
            if h.get("aio-tests-csrf-token"):
                captured["AIO_CSRF"] = h["aio-tests-csrf-token"]
            if h.get("x-aio-tests-forge-session"):
                captured["AIO_SESSION"] = h["x-aio-tests-forge-session"]
            if all(k in captured for k in ("AIO_TOKEN", "AIO_CSRF", "AIO_SESSION")):
                got_tokens.set()

        async def wait_or_tokens(ms: int) -> None:
            """Sleep up to `ms`, returning the instant all three headers are seen."""
            try:
                await asyncio.wait_for(got_tokens.wait(), timeout=ms / 1000)
            except asyncio.TimeoutError:
                pass

        async def on_any_request(request):
            from urllib.parse import urlparse
            seen_domains.add(urlparse(request.url).netloc)

        context.on("request", on_request)
        context.on("request", on_any_request)
        page = await context.new_page()
        working_url = None
        cached_selector = _config._get("AIO_PANEL_SELECTOR")
        working_selector = None

        for url in candidates:
            if captured.get("AIO_TOKEN"):
                break

            print(f"  AIO refresh: trying {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await wait_or_tokens(2_000)

                if not logged_in and _is_login_page(page.url):
                    # Stale state redirects to login/join — delete it and start fresh
                    if _STATE_FILE.exists():
                        _STATE_FILE.unlink()
                        print("  AIO refresh: deleted stale browser state")

                    print(f"  AIO refresh: login required — signing in as {_config.JIRA_EMAIL}...")
                    try:
                        await _do_login(page, _config)
                        await context.storage_state(path=str(_STATE_FILE))
                        logged_in = True
                        print("  AIO refresh: login succeeded, session saved")
                    except Exception as exc:
                        print(f"  AIO refresh: login failed — {exc}")
                        # If still on a login page, headless reCAPTCHA likely blocked us
                        if _is_login_page(page.url):
                            login_was_blocked = True

                # Re-navigate to the story if SSO redirected us away
                if url not in page.url:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    await wait_or_tokens(3_000)

                # Wait for the Jira SPA to settle — skipped once tokens are in hand.
                # Jira rarely reaches networkidle, so cap it low instead of burning 15 s.
                if not got_tokens.is_set():
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5_000)
                    except Exception:
                        pass

                # Try to click the AIO panel trigger — Forge panels don't fire API
                # requests until they're visible in Jira's right sidebar.
                selectors = [
                    "button:has-text('AIO')",
                    "[data-testid*='aio']",
                    "[data-extension-id*='aio']",
                    "[aria-label*='AIO']",
                    "button:has-text('Tests')",
                    "[data-testid='issue.views.field.glance.panel-header']:has-text('AIO')",
                    "button:has-text('Apps')",
                ]
                if cached_selector in selectors:
                    selectors.remove(cached_selector)
                    selectors.insert(0, cached_selector)

                for sel in selectors:
                    if got_tokens.is_set():
                        break
                    try:
                        el = page.locator(sel).first
                        await el.wait_for(state="visible", timeout=1_500)
                        await el.click()
                        print(f"  AIO refresh: clicked panel trigger ({sel})")
                        working_selector = sel
                        await wait_or_tokens(5_000)
                        break
                    except Exception:
                        pass

                print(f"  AIO refresh: on {page.url[:80]} — waiting for AIO requests...")
                await wait_or_tokens(25_000)

                if captured.get("AIO_TOKEN"):
                    working_url = url
                else:
                    domains = ", ".join(sorted(seen_domains)[:15])
                    print(f"  AIO refresh: no tokens from {url}")
                    print(f"  AIO refresh: domains seen: {domains}")

            except Exception as exc:
                print(f"  AIO refresh: error on {url} — {exc}")

        if captured.get("AIO_TOKEN"):
            await context.storage_state(path=str(_STATE_FILE))
            import db
            if working_url and working_url != configured:
                db.setting_set("AIO_PAGE_URL", working_url)
                os.environ["AIO_PAGE_URL"] = working_url
                print(f"  AIO refresh: cached working URL → {working_url}")
            if working_selector and working_selector != cached_selector:
                db.setting_set("AIO_PANEL_SELECTOR", working_selector)
                print(f"  AIO refresh: cached panel selector → {working_selector}")

        await browser.close()

        # Signal caller to retry with a visible browser so reCAPTCHA doesn't block login
        if not captured.get("AIO_TOKEN") and login_was_blocked and headless:
            raise _LoginBlockedError("headless login blocked — will retry with visible browser")

        return captured


def refresh_tokens() -> bool:
    with _lock:
        print("  AIO: refreshing tokens via Playwright...")
        try:
            loop = asyncio.new_event_loop()
            try:
                tokens = loop.run_until_complete(_capture_async(headless=True))
            except _LoginBlockedError as exc:
                print(f"  AIO: {exc}")
                print("  AIO: retrying with visible browser (reCAPTCHA bypass)...")
                tokens = loop.run_until_complete(_capture_async(headless=False))
            finally:
                loop.close()
        except Exception as exc:
            print(f"  AIO: token refresh failed — {exc}")
            return False

        if not tokens.get("AIO_TOKEN"):
            print("  AIO: no tokens captured — all candidate Story URLs failed")
            return False

        import db
        for key, value in tokens.items():
            db.setting_set(key, value)
            os.environ[key] = value

        print("  AIO: tokens refreshed successfully")
        return True


def start_background_refresh() -> None:
    import config
    if not config.JIRA_PASSWORD:
        print("  AIO: auto-refresh disabled — set JIRA_PASSWORD in .env")
        return

    threading.Thread(target=refresh_tokens, daemon=True, name="aio-token-refresh-init").start()

    def _loop():
        while True:
            time.sleep(_REFRESH_INTERVAL)
            try:
                refresh_tokens()
            except Exception as exc:
                print(f"  AIO: background refresh error — {exc}")

    threading.Thread(target=_loop, daemon=True, name="aio-token-refresh").start()
    print("  AIO: token auto-refresh enabled (every 40 min)")
