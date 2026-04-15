"""
ai-commander: thin MCP server bridging Claude to Gemini and Codex.

Exposes two tools:
  - ask_gemini(prompt) → Gemini via ACP protocol (JSON-RPC over stdio)
  - ask_codex(prompt)  → Codex via app-server protocol (JSON-RPC over stdio)

Architecture copied from Roundtable (claudable_helper/cli/adapters).
Both providers use a shared _ACPClient (JSON-RPC 2.0 over NDJSON/stdio).
Per-call subprocess isolation — each tool call spawns a fresh process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP

# ── Logging ─────────────────────────────────────────────────────────────────

LOG_DIR = os.path.expanduser(os.getenv("LOG_DIR", "~/.ai-commander/logs"))
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("ai-commander")
logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(os.path.join(LOG_DIR, "server.log"))
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_file_handler)

# ── Configuration ────────────────────────────────────────────────────────────

GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "180"))
CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "300"))
GEMINI_MAX_CONCURRENT = int(os.getenv("GEMINI_MAX_CONCURRENT", "4"))
CODEX_MAX_CONCURRENT = int(os.getenv("CODEX_MAX_CONCURRENT", "4"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
READLINE_TIMEOUT = int(os.getenv("READLINE_TIMEOUT", "120"))
MAX_CONSECUTIVE_TIMEOUTS = int(os.getenv("MAX_CONSECUTIVE_TIMEOUTS", "3"))

_gemini_sem: Optional[asyncio.Semaphore] = None
_codex_sem: Optional[asyncio.Semaphore] = None


def _get_gemini_sem() -> asyncio.Semaphore:
    global _gemini_sem
    if _gemini_sem is None:
        _gemini_sem = asyncio.Semaphore(GEMINI_MAX_CONCURRENT)
    return _gemini_sem


def _get_codex_sem() -> asyncio.Semaphore:
    global _codex_sem
    if _codex_sem is None:
        _codex_sem = asyncio.Semaphore(CODEX_MAX_CONCURRENT)
    return _codex_sem


# ── CLI Availability Check ──────────────────────────────────────────────────

def _check_cli_available(name: str) -> bool:
    """Check if a CLI tool is installed and on PATH."""
    return shutil.which(name) is not None


def _ensure_cli(name: str) -> None:
    """Raise immediately if the CLI is not installed."""
    if not _check_cli_available(name):
        raise RuntimeError(
            f"'{name}' CLI not found on PATH. "
            f"Install it first: see README.md for instructions."
        )


# ── LineBuffer (from Roundtable cli/base.py) ─────────────────────────────────

class LineBuffer:
    """Async line reader that handles arbitrary line lengths.

    Reads in 8KB chunks until newline, solving asyncio StreamReader's
    64KB buffer limit for large NDJSON responses.
    """

    def __init__(self, stream: asyncio.StreamReader):
        self.stream = stream
        self.buffer = b""

    async def readline(self) -> bytes:
        while b"\n" not in self.buffer:
            chunk = await self.stream.read(8192)
            if not chunk:
                line = self.buffer
                self.buffer = b""
                return line
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line + b"\n"


# ── _ACPClient (from Roundtable cli/adapters/qwen_cli.py) ───────────────────

@dataclass
class _Pending:
    fut: asyncio.Future


class _ACPClient:
    """Minimal JSON-RPC 2.0 client over newline-delimited JSON on stdio.

    Handles:
    - Outgoing requests with ID tracking and futures
    - Incoming responses routed to pending futures
    - Incoming notifications dispatched to handlers
    - Incoming server-side requests answered by registered handlers
    - Per-readline timeout with consecutive timeout kill
    """

    def __init__(self, cmd: List[str], env: Optional[Dict[str, str]] = None):
        self._cmd = cmd
        self._env = env or os.environ.copy()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: Dict[int, _Pending] = {}
        self._notif_handlers: Dict[str, List[Callable]] = {}
        self._request_handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def stop(self) -> None:
        try:
            for pending in self._pending.values():
                if not pending.fut.done():
                    pending.fut.cancel()
            self._pending.clear()
            if self._proc and self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        finally:
            self._proc = None
            if self._reader_task:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except asyncio.CancelledError:
                    pass
                self._reader_task = None
            if self._stderr_task:
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except asyncio.CancelledError:
                    pass
                self._stderr_task = None

    def on_notification(self, method: str, handler: Callable) -> None:
        self._notif_handlers.setdefault(method, []).append(handler)

    def off_notification(self, method: str, handler: Optional[Callable] = None) -> None:
        if method not in self._notif_handlers:
            return
        if handler is None:
            self._notif_handlers[method] = []
        else:
            self._notif_handlers[method] = [
                h for h in self._notif_handlers[method] if h != handler
            ]

    def on_request(self, method: str, handler: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]) -> None:
        self._request_handlers[method] = handler

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACP process not started")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = _Pending(fut=fut)
        obj = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        data = (json.dumps(obj) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        return await fut

    async def send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACP process not started")
        obj = {"jsonrpc": "2.0", "method": method}
        if params:
            obj["params"] = params
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _reader_loop(self) -> None:
        consecutive_timeouts = 0
        try:
            if not self._proc or not self._proc.stdout:
                return
            reader = LineBuffer(self._proc.stdout)
            while True:
                try:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=READLINE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    consecutive_timeouts += 1
                    logger.warning(
                        "[READER] readline timeout (%d/%d) for %s",
                        consecutive_timeouts, MAX_CONSECUTIVE_TIMEOUTS, self._cmd,
                    )
                    if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                        logger.error(
                            "[READER] %d consecutive timeouts, killing process %s",
                            MAX_CONSECUTIVE_TIMEOUTS, self._cmd,
                        )
                        if self._proc and self._proc.returncode is None:
                            self._proc.kill()
                        break
                    continue

                if not line:
                    break
                consecutive_timeouts = 0
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:
                    logger.debug("[READER] non-JSON line from %s: %s", self._cmd[0], line[:200])
                    continue

                if not isinstance(msg, dict):
                    continue

                # Response to our request
                if "id" in msg and "method" not in msg:
                    slot = self._pending.pop(int(msg["id"]), None) if isinstance(msg.get("id"), (int, str)) else None
                    if not slot:
                        continue
                    if "error" in msg:
                        slot.fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        slot.fut.set_result(msg.get("result"))
                    continue

                # Request from agent (client-side)
                if "method" in msg and "id" in msg:
                    req_id = msg["id"]
                    handler = self._request_handlers.get(msg["method"])
                    if handler:
                        try:
                            result = await handler(msg.get("params") or {})
                            await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
                        except Exception as e:
                            await self._send({
                                "jsonrpc": "2.0", "id": req_id,
                                "error": {"code": -32000, "message": str(e)},
                            })
                    else:
                        await self._send({
                            "jsonrpc": "2.0", "id": req_id,
                            "error": {"code": -32601, "message": "Method not found"},
                        })
                    continue

                # Notification from agent
                if "method" in msg and "id" not in msg:
                    for h in self._notif_handlers.get(msg["method"], []):
                        try:
                            h(msg.get("params") or {})
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[READER] unexpected error in reader loop: %s", exc)
        finally:
            for pending in self._pending.values():
                if not pending.fut.done():
                    pending.fut.set_exception(
                        RuntimeError("Process exited unexpectedly")
                    )

    async def _send(self, obj: Dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            return
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _stderr_loop(self) -> None:
        """Background task to drain stderr and log it."""
        try:
            if not self._proc or not self._proc.stderr:
                return
            reader = LineBuffer(self._proc.stderr)
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("[STDERR:%s] %s", self._cmd[0], text)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


# ── Gemini via ACP (from Roundtable cli/adapters/gemini_cli.py) ──────────────

async def _call_gemini(prompt: str, ctx: Optional[Context] = None) -> str:
    """Call Gemini CLI via ACP protocol. Per-call subprocess isolation."""
    _ensure_cli("gemini")
    env = os.environ.copy()
    env.setdefault("NO_BROWSER", "1")
    client = _ACPClient(["gemini", "--acp"], env=env)

    # Auto-approve permission requests (from Roundtable)
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
        return {"content": ""}

    async def _fs_write(params: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    client.on_request("session/request_permission", _handle_permission)
    client.on_request("fs/read_text_file", _fs_read)
    client.on_request("fs/write_text_file", _fs_write)

    try:
        await client.start()
        logger.info("[GEMINI] process started, initializing ACP")

        # Initialize
        await client.request("initialize", {
            "clientInfo": {"name": "ai-commander", "version": "0.1.0"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "protocolVersion": 1,
        })

        # Create session
        result = await client.request("session/new", {"cwd": os.getcwd(), "mcpServers": []})
        session_id = result.get("sessionId")
        if not session_id:
            raise RuntimeError("Failed to create Gemini session")

        # Collect response text via notification handler (from Roundtable)
        text_chunks: List[str] = []
        chunk_count = 0

        def _on_update(params: Dict[str, Any]) -> None:
            nonlocal chunk_count
            if params.get("sessionId") != session_id:
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate") or update.get("type")
            if kind in ("agent_message_chunk", "agent_thought_chunk"):
                text = ((update.get("content") or {}).get("text")) or update.get("text") or ""
                if isinstance(text, str) and text:
                    text_chunks.append(text)
                    chunk_count += 1

        client.on_notification("session/update", _on_update)
        try:
            if ctx:
                await ctx.report_progress(0, 1)
            # Send prompt and wait (from Roundtable)
            try:
                await asyncio.wait_for(
                    client.request("session/prompt", {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    }),
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
        await client.stop()


# ── Codex via App-Server ─────────────────────────────────────────────────────

async def _call_codex(prompt: str, ctx: Optional[Context] = None) -> str:
    """Call Codex CLI via app-server (JSON-RPC 2.0). Per-call subprocess isolation.

    Protocol: initialize → initialized → thread/start → turn/start →
              item/agentMessage/delta notifications → turn/completed
    """
    _ensure_cli("codex")
    client = _ACPClient(["codex", "app-server"])

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

    client.on_notification("item/agentMessage/delta", _on_agent_delta)
    client.on_notification("turn/completed", _on_turn_completed)

    try:
        await client.start()
        logger.info("[CODEX] process started, initializing app-server")

        # 1. Initialize handshake
        await client.request("initialize", {
            "clientInfo": {
                "name": "ai_commander",
                "title": "AI Commander MCP",
                "version": "0.1.0",
            },
            "capabilities": {},
        })

        # 2. Send initialized notification (required by protocol)
        await client.send_notification("initialized")

        # 3. Create thread with auto-approval
        thread_result = await client.request("thread/start", {
            "cwd": os.getcwd(),
            "approvalPolicy": "never",
            "sandbox": {"type": "workspaceWrite"},
        })
        thread_id = (thread_result or {}).get("thread", {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex app-server: failed to create thread")

        # 4. Start turn with user prompt
        turn_done.clear()
        if ctx:
            await ctx.report_progress(0, 1)
        await client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        })

        # 5. Wait for turn/completed notification
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

mcp = FastMCP("ai-commander")


@mcp.tool()
async def ask_gemini(prompt: str, ctx: Context) -> str:
    """Send a prompt to Gemini (gemini-2.5-pro) and return its response."""
    errors: List[str] = []
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.monotonic()
        try:
            async with _get_gemini_sem():
                result = await _call_gemini(prompt, ctx)
            logger.info("[TOOL] ask_gemini OK in %.1fs", time.monotonic() - t0)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            errors.append(f"Attempt {attempt + 1}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}")
            logger.warning("[TOOL] ask_gemini attempt %d failed in %.1fs: %s", attempt + 1, elapsed, exc)
    return "[ERROR] Gemini failed after {} attempts:\n{}".format(MAX_ATTEMPTS, "\n".join(errors))


@mcp.tool()
async def ask_codex(prompt: str, ctx: Context) -> str:
    """Send a prompt to Codex (gpt-5.4) and return its response."""
    errors: List[str] = []
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.monotonic()
        try:
            async with _get_codex_sem():
                result = await _call_codex(prompt, ctx)
            logger.info("[TOOL] ask_codex OK in %.1fs", time.monotonic() - t0)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            errors.append(f"Attempt {attempt + 1}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}")
            logger.warning("[TOOL] ask_codex attempt %d failed in %.1fs: %s", attempt + 1, elapsed, exc)
    return "[ERROR] Codex failed after {} attempts:\n{}".format(MAX_ATTEMPTS, "\n".join(errors))


if __name__ == "__main__":
    logger.info("ai-commander starting")
    mcp.run()
