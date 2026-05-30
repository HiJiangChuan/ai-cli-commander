"""
claude-server: MCP server bridging agents to Claude Code CLI via headless mode.

Protocol: CLI stdout JSON (claude -p --output-format json --bare).
Per-call subprocess isolation — each tool call spawns a fresh claude process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import List, Optional

from mcp.server.fastmcp import Context, FastMCP

from ai_commander.core import ensure_cli, get_semaphore, setup_logging

logger = logging.getLogger("ai_commander.claude")

# ── Configuration ────────────────────────────────────────────────────────────

CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))
CLAUDE_MAX_CONCURRENT = int(os.getenv("CLAUDE_MAX_CONCURRENT", "4"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))


# ── Claude via headless CLI ──────────────────────────────────────────────────


async def _call_claude(
    prompt: str,
    ctx: Optional[Context] = None,
    max_turns: int = 5,
    max_budget_usd: float = 0.50,
    allowed_tools: str = "Read,Bash,Edit",
) -> str:
    """Call Claude Code CLI in headless mode. Per-call subprocess isolation."""
    ensure_cli("claude")

    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--bare",
        "--dangerously-skip-permissions",
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        str(max_budget_usd),
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])

    logger.info("[CLAUDE] spawning: %s", " ".join(cmd))

    if ctx:
        await ctx.report_progress(0, 1)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=CLAUDE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise RuntimeError(f"Claude timed out after {CLAUDE_TIMEOUT}s")

    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if stderr_text:
        logger.debug("[CLAUDE STDERR] %s", stderr_text[:500])

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    if not stdout_text:
        raise RuntimeError("Claude returned empty stdout")

    # Parse JSON output
    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError as e:
        # Sometimes Claude may print extra lines before/after JSON;
        # try to extract the first JSON object.
        lines = stdout_text.splitlines()
        data = None
        for line in lines:
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if data is None:
            raise RuntimeError(f"Claude returned invalid JSON: {e}")

    if not isinstance(data, dict):
        raise RuntimeError("Claude returned unexpected non-JSON output")

    # Check for error indication in output
    result_text = data.get("result") or data.get("response") or ""
    if data.get("error"):
        raise RuntimeError(f"Claude error: {data['error']}")

    # Claude may exit with non-zero even on success in some edge cases,
    # so we prioritize parsed JSON over return code.
    if proc.returncode != 0 and not result_text:
        raise RuntimeError(f"Claude exited with code {proc.returncode}")

    if ctx:
        await ctx.report_progress(1, 1)

    cost = data.get("cost_usd")
    duration = data.get("duration_ms")
    logger.info("[CLAUDE] OK cost=$%s duration=%sms", cost, duration)

    return str(result_text).strip() or "[No response from Claude]"


# ── FastMCP Server ───────────────────────────────────────────────────────────

mcp = FastMCP("claude-server")


@mcp.tool()
async def ask_claude(
    prompt: str,
    ctx: Context,
    max_turns: int = 5,
    max_budget_usd: float = 0.50,
    allowed_tools: str = "Read,Bash,Edit",
) -> str:
    """Send a prompt to Claude Code (Sonnet 4.6) and return its response.

    Args:
        prompt: The task or question for Claude.
        max_turns: Max agent loop turns (default 5).
        max_budget_usd: Max spend per call in USD (default 0.50).
        allowed_tools: Comma-separated allowed tools (default "Read,Bash,Edit").
    """
    errors: List[str] = []
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.monotonic()
        try:
            async with get_semaphore("claude", CLAUDE_MAX_CONCURRENT):
                result = await _call_claude(
                    prompt, ctx, max_turns, max_budget_usd, allowed_tools
                )
            logger.info("[TOOL] ask_claude OK in %.1fs", time.monotonic() - t0)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            errors.append(
                f"Attempt {attempt + 1}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}"
            )
            logger.warning(
                "[TOOL] ask_claude attempt %d failed in %.1fs: %s",
                attempt + 1,
                elapsed,
                exc,
            )
    return "[ERROR] Claude failed after {} attempts:\n{}".format(
        MAX_ATTEMPTS, "\n".join(errors)
    )


def main():
    setup_logging("ai_commander.claude")
    logger.info("claude-server starting")
    mcp.run()


if __name__ == "__main__":
    main()
