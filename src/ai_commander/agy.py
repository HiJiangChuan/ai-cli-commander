"""
agy-server: MCP server bridging agents to Antigravity CLI via --print mode.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import List, Optional

from mcp.server.fastmcp import Context, FastMCP

from ai_commander.core import ensure_cli, get_semaphore, setup_logging

logger = logging.getLogger("ai_commander.agy")

AGY_TIMEOUT = int(os.getenv("AGY_TIMEOUT", "180"))
AGY_MAX_CONCURRENT = int(os.getenv("AGY_MAX_CONCURRENT", "4"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))


async def _call_agy(prompt: str, ctx: Optional[Context] = None) -> str:
    ensure_cli("agy")
    proc = await asyncio.create_subprocess_exec(
        "agy", "--print", prompt, "--dangerously-skip-permissions",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        if ctx:
            await ctx.report_progress(0, 1)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=AGY_TIMEOUT)
        if ctx:
            await ctx.report_progress(1, 1)
        return stdout.decode("utf-8", errors="replace").strip() or "[No response from Antigravity CLI]"
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Antigravity CLI timed out after {AGY_TIMEOUT}s")


mcp = FastMCP("agy-server")


@mcp.tool()
async def ask_agy(prompt: str, ctx: Context) -> str:
    """Send a prompt to Antigravity CLI (Gemini 3.5 Flash) and return its response."""
    errors: List[str] = []
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.monotonic()
        try:
            async with get_semaphore("agy", AGY_MAX_CONCURRENT):
                result = await _call_agy(prompt, ctx)
            logger.info("[TOOL] ask_agy OK in %.1fs", time.monotonic() - t0)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            errors.append(f"Attempt {attempt + 1}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}")
            logger.warning("[TOOL] ask_agy attempt %d failed in %.1fs: %s", attempt + 1, elapsed, exc)
    return "[ERROR] Antigravity CLI failed after {} attempts:\n{}".format(
        MAX_ATTEMPTS, "\n".join(errors)
    )


def main():
    setup_logging("ai_commander.agy")
    logger.info("agy-server starting")
    mcp.run()


if __name__ == "__main__":
    main()
