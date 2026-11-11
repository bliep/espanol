"""Web client waits long enough for a sleeping (cold-starting) agent.

Run: start web dev with an agent name nobody serves, so the agent never joins:
    cd web && AGENT_NAME=tutor-absent npx pnpm@9.15.9 dev
    cd agent && uv run python evals/agent_wait.py

On LiveKit Cloud's Build plan the deployed agent sleeps when idle; waking it takes
10–20 s plus model loading. The page must still be waiting after WAIT_S, not show
"Session ended" (LiveKit's default agent timeout is 20 s).

Needs Chromium for Playwright: uv run playwright install chromium
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import create_user, delete_user  # noqa: E402

WEB_URL = os.environ.get("WEB_URL", "http://localhost:3000")
SUPABASE_URL = os.environ["SUPABASE_URL"]
PROJECT_REF = SUPABASE_URL.split("//")[1].split(".")[0]
WAIT_S = 35  # beyond the 20 s default, within a typical cold start
ENDED = "Session ended"  # web/hooks/useAgentErrors.tsx toast
POLL_MS = 1000
CONNECT_TIMEOUT_MS = 15_000  # room connected (mode picker shown), agent not yet joined


async def session_for(http: aiohttp.ClientSession) -> tuple[str, dict]:
    """Throwaway learner and the Supabase session the browser stores after login."""
    user_id, access_token = await create_user(http)
    return user_id, {"access_token": access_token, "token_type": "bearer",
                     "expires_at": int(time.time()) + 3600, "refresh_token": "",
                     "user": {"id": user_id}}  # fmt: skip


async def main() -> int:
    async with aiohttp.ClientSession() as http:
        user_id, session = await session_for(http)
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
                )
                context = await browser.new_context(permissions=["microphone"])
                key = json.dumps(f"sb-{PROJECT_REF}-auth-token")
                await context.add_init_script(
                    f"localStorage.setItem({key}, {json.dumps(json.dumps(session))});"
                )
                page = await context.new_page()
                await page.goto(WEB_URL)
                await page.get_by_role("button", name="Start call").click()

                # Watch the whole time: the toast only shows for 10 s, and giving up
                # also brings the "Start call" screen back.
                ended, waited = False, 0
                await page.get_by_text("Mode", exact=True).wait_for(timeout=CONNECT_TIMEOUT_MS)
                start = page.get_by_role("button", name="Start call")
                await start.wait_for(state="detached")  # welcome view fades out first
                while waited < WAIT_S * 1000 and not ended:
                    toast = await page.get_by_text(ENDED).count() > 0
                    restart = await page.get_by_role("button", name="Start call").count() > 0
                    ended = toast or restart
                    await page.wait_for_timeout(POLL_MS)
                    waited += POLL_MS
                await browser.close()
        finally:
            await delete_user(http, user_id)

    verdict = f"gave up after ~{waited / 1000:.0f} s" if ended else "still waiting"
    print(f"no agent for {WAIT_S} s: {verdict}")
    return 1 if ended else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
