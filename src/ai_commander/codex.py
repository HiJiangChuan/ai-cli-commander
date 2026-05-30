"""
codex-server: MCP server bridging agents to Codex CLI via app-server protocol.

Protocol: JSON-RPC 2.0 over NDJSON/stdio (app-server).
Per-call subprocess isolation — each tool call spawns a fresh codex process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP

from ai_commander.core import ACPClient, ensure_cli, get_semaphore, setup_logging

logger = logging.getLogger("ai_commander.codex")

# ── Configuration ────────────────────────────────────────────────────────────

CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "300"))
CODEX_MAX_CONCURRENT = int(os.getenv("CODEX_MAX_CONCURRENT", "4"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))


# ── Codex via App-Server ─────────────────────────────────────────────────────


async def _call_codex(prompt: str, ctx: Optional[Context] = None) -> str:
    """Call Codex CLI via app-server (JSON-RPC 2.0). Per-call subprocess isolation."""
    ensure_cli("codex")
    client = ACPClient(["codex", "app-server"], logger=logger)

    text_chunks: List[str] = []
    turn_done = asyncio.Event()
    turn_error: List[str] = []
    chunk_count = 0

    def _on_agent_delta(params: Dict[str, Any]) -> None:
        nonlocal chunk_count
        delta = params.get("delta", "")
        if isinstance(delta, str) and delta:
            text_chunks.append(delta)
            chunk_count += 1

    def _on_turn_completed(params: Dict[str, Any]) -> None:
        turn = params.get("turn") or {}
        status = turn.get("status", "")
        if status == "failed":
            turn_error.append(turn.get("error", "Turn failed"))
        turn_done.set()

    # Auto-approve any approval requests (safety net even with approvalPolicy "never")
    async def _auto_approve(params: Dict[str, Any]) -> Dict[str, Any]:
        logger.debug(
            "[CODEX] auto-approving request: %s",
            params.get("command", params.get("path", "")),
        )
        return {"decision": "accept"}

    client.on_notification("item/agentMessage/delta", _on_agent_delta)
    client.on_notification("turn/completed", _on_turn_completed)
    client.on_request("item/commandExecution/requestApproval", _auto_approve)
    client.on_request("item/fileChange/requestApproval", _auto_approve)
    client.on_request("item/permissions/requestApproval", _auto_approve)

    try:
        await client.start()
        logger.info("[CODEX] process started, initializing app-server")

        await client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-server",
                    "title": "Codex MCP",
                    "version": "0.2.0",
                },
                "capabilities": {},
            },
        )
        await client.send_notification("initialized")

        thread_result = await client.request(
            "thread/start",
            {
                "cwd": os.getcwd(),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
            },
        )
        thread_id = (thread_result or {}).get("thread", {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex app-server: failed to create thread")

        turn_done.clear()
        if ctx:
            await ctx.report_progress(0, 1)
        await client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )

        try:
            await asyncio.wait_for(turn_done.wait(), timeout=CODEX_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Codex timed out after {CODEX_TIMEOUT}s")

        if turn_error:
            raise RuntimeError(f"Codex error: {turn_error[0]}")

        if ctx:
            await ctx.report_progress(1, 1)
        return "".join(text_chunks).strip() or "[No response from Codex]"
    finally:
        await client.stop()


# ── FastMCP Server ───────────────────────────────────────────────────────────

mcp = FastMCP("codex-server")


@mcp.tool()
async def ask_codex(prompt: str, ctx: Context) -> str:
    """Send a prompt to Codex (gpt-5.4) and return its response."""
    errors: List[str] = []
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.monotonic()
        try:
            async with get_semaphore("codex", CODEX_MAX_CONCURRENT):
                result = await _call_codex(prompt, ctx)
            logger.info("[TOOL] ask_codex OK in %.1fs", time.monotonic() - t0)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            errors.append(
                f"Attempt {attempt + 1}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}"
            )
            logger.warning(
                "[TOOL] ask_codex attempt %d failed in %.1fs: %s",
                attempt + 1,
                elapsed,
                exc,
            )
    return "[ERROR] Codex failed after {} attempts:\n{}".format(
        MAX_ATTEMPTS, "\n".join(errors)
    )


def main():
    setup_logging("ai_commander.codex")
    logger.info("codex-server starting")
    mcp.run()


if __name__ == "__main__":
    main()
