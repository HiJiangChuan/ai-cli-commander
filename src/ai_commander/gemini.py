"""
gemini-server: MCP server bridging agents to Gemini CLI via ACP protocol.

Protocol: JSON-RPC 2.0 over NDJSON/stdio (ACP).
Per-call subprocess isolation — each tool call spawns a fresh gemini process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP

from ai_commander.core import ACPClient, ensure_cli, get_semaphore, setup_logging

logger = logging.getLogger("ai_commander.gemini")

# ── Configuration ────────────────────────────────────────────────────────────

GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "180"))
GEMINI_MAX_CONCURRENT = int(os.getenv("GEMINI_MAX_CONCURRENT", "4"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))


# ── Gemini via ACP ───────────────────────────────────────────────────────────


async def _call_gemini(prompt: str, ctx: Optional[Context] = None) -> str:
    """Call Gemini CLI via ACP protocol. Per-call subprocess isolation."""
    ensure_cli("gemini")
    env = os.environ.copy()
    env.setdefault("GEMINI_CLI_NO_BROWSER", "true")
    env.setdefault("GEMINI_CLI_NO_RELAUNCH", "true")
    client = ACPClient(
        ["gemini", "--acp", "--approval-mode", "yolo", "--skip-trust"],
        env=env,
        logger=logger,
    )

    # ── Client-side request handlers (Gemini CLI calls us) ──────────────

    async def _handle_permission(params: Dict[str, Any]) -> Dict[str, Any]:
        options = params.get("options") or []
        chosen = None
        for kind in ("allow_always", "allow_once"):
            chosen = next((o for o in options if o.get("kind") == kind), None)
            if chosen:
                break
        if not chosen and options:
            chosen = options[0]
        if not chosen:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    async def _fs_read(params: Dict[str, Any]) -> Dict[str, Any]:
        path = params.get("path") or params.get("filePath") or ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return {"content": f.read()}
        except Exception:
            return {"content": ""}

    async def _fs_write(params: Dict[str, Any]) -> Dict[str, Any]:
        path = params.get("path") or params.get("filePath") or ""
        content = params.get("content") or ""
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass
        return {}

    # Terminal handlers — Gemini CLI sends terminal/* requests for shell cmds.
    # We must respond or it hangs.
    _terminals: Dict[str, asyncio.subprocess.Process] = {}

    async def _terminal_create(params: Dict[str, Any]) -> Dict[str, Any]:
        cmd = params.get("command") or params.get("cmd") or "bash"
        cwd = params.get("cwd") or os.getcwd()
        terminal_id = str(len(_terminals) + 1)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _terminals[terminal_id] = proc
        return {"terminalId": terminal_id}

    async def _terminal_output(params: Dict[str, Any]) -> Dict[str, Any]:
        tid = params.get("terminalId") or ""
        proc = _terminals.get(tid)
        if not proc or not proc.stdout:
            return {"output": ""}
        try:
            data = await asyncio.wait_for(proc.stdout.read(65536), timeout=5.0)
            return {"output": data.decode("utf-8", errors="replace")}
        except asyncio.TimeoutError:
            return {"output": ""}

    async def _terminal_wait(params: Dict[str, Any]) -> Dict[str, Any]:
        tid = params.get("terminalId") or ""
        proc = _terminals.get(tid)
        if not proc:
            return {"exitCode": -1}
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=60.0)
            return {"exitCode": code}
        except asyncio.TimeoutError:
            return {"exitCode": -1}

    async def _terminal_kill(params: Dict[str, Any]) -> Dict[str, Any]:
        tid = params.get("terminalId") or ""
        proc = _terminals.pop(tid, None)
        if proc and proc.returncode is None:
            proc.kill()
        return {}

    async def _terminal_release(params: Dict[str, Any]) -> Dict[str, Any]:
        tid = params.get("terminalId") or ""
        proc = _terminals.pop(tid, None)
        if proc and proc.returncode is None:
            proc.kill()
        return {}

    client.on_request("session/request_permission", _handle_permission)
    client.on_request("fs/read_text_file", _fs_read)
    client.on_request("fs/write_text_file", _fs_write)
    client.on_request("terminal/create", _terminal_create)
    client.on_request("terminal/output", _terminal_output)
    client.on_request("terminal/wait_for_exit", _terminal_wait)
    client.on_request("terminal/kill", _terminal_kill)
    client.on_request("terminal/release", _terminal_release)

    try:
        await client.start()
        logger.info("[GEMINI] process started, initializing ACP")

        await client.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "gemini-server", "version": "0.2.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            },
        )

        result = await client.request(
            "session/new", {"cwd": os.getcwd(), "mcpServers": []}
        )
        session_id = result.get("sessionId")
        if not session_id:
            raise RuntimeError("Failed to create Gemini session")

        text_chunks: List[str] = []
        chunk_count = 0

        def _on_update(params: Dict[str, Any]) -> None:
            nonlocal chunk_count
            if params.get("sessionId") != session_id:
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate") or update.get("type")
            if kind in ("agent_message_chunk", "agent_thought_chunk"):
                text = (
                    ((update.get("content") or {}).get("text"))
                    or update.get("text")
                    or ""
                )
                if isinstance(text, str) and text:
                    text_chunks.append(text)
                    chunk_count += 1

        client.on_notification("session/update", _on_update)
        try:
            if ctx:
                await ctx.report_progress(0, 1)
            try:
                await asyncio.wait_for(
                    client.request(
                        "session/prompt",
                        {
                            "sessionId": session_id,
                            "prompt": [{"type": "text", "text": prompt}],
                        },
                    ),
                    timeout=GEMINI_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Gemini timed out after {GEMINI_TIMEOUT}s")

            if ctx:
                await ctx.report_progress(1, 1)
            return "".join(text_chunks).strip() or "[No response from Gemini]"
        finally:
            client.off_notification("session/update", _on_update)
    finally:
        # Clean up any lingering terminal processes
        for proc in _terminals.values():
            if proc.returncode is None:
                proc.kill()
        _terminals.clear()
        await client.stop()


# ── FastMCP Server ───────────────────────────────────────────────────────────

mcp = FastMCP("gemini-server")


@mcp.tool()
async def ask_gemini(prompt: str, ctx: Context) -> str:
    """Send a prompt to Gemini (gemini-2.5-pro) and return its response."""
    errors: List[str] = []
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.monotonic()
        try:
            async with get_semaphore("gemini", GEMINI_MAX_CONCURRENT):
                result = await _call_gemini(prompt, ctx)
            logger.info("[TOOL] ask_gemini OK in %.1fs", time.monotonic() - t0)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            errors.append(
                f"Attempt {attempt + 1}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}"
            )
            logger.warning(
                "[TOOL] ask_gemini attempt %d failed in %.1fs: %s",
                attempt + 1,
                elapsed,
                exc,
            )
    return "[ERROR] Gemini failed after {} attempts:\n{}".format(
        MAX_ATTEMPTS, "\n".join(errors)
    )


def main():
    setup_logging("ai_commander.gemini")
    logger.info("gemini-server starting")
    mcp.run()


if __name__ == "__main__":
    main()
