"""
codex-server: MCP server bridging agents to Codex CLI via app-server protocol.

Protocol: JSON-RPC 2.0 over NDJSON/stdio (app-server).
Per-call subprocess isolation — each tool call spawns a fresh codex process.
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

logger = logging.getLogger("codex-server")
logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(os.path.join(LOG_DIR, "codex-server.log"))
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_file_handler)

# ── Configuration ────────────────────────────────────────────────────────────

CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "300"))
CODEX_MAX_CONCURRENT = int(os.getenv("CODEX_MAX_CONCURRENT", "4"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
READLINE_TIMEOUT = int(os.getenv("READLINE_TIMEOUT", "120"))
MAX_CONSECUTIVE_TIMEOUTS = int(os.getenv("MAX_CONSECUTIVE_TIMEOUTS", "3"))

_codex_sem: Optional[asyncio.Semaphore] = None


def _get_codex_sem() -> asyncio.Semaphore:
    global _codex_sem
    if _codex_sem is None:
        _codex_sem = asyncio.Semaphore(CODEX_MAX_CONCURRENT)
    return _codex_sem


# ── CLI Availability ────────────────────────────────────────────────────────

def _check_cli_available(name: str) -> bool:
    return shutil.which(name) is not None


def _ensure_cli(name: str) -> None:
    if not _check_cli_available(name):
        raise RuntimeError(f"'{name}' CLI not found on PATH. Install it first.")


# ── LineBuffer ──────────────────────────────────────────────────────────────

class LineBuffer:
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


# ── _ACPClient ──────────────────────────────────────────────────────────────

@dataclass
class _Pending:
    fut: asyncio.Future


class _ACPClient:
    """Minimal JSON-RPC 2.0 client over newline-delimited JSON on stdio."""

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
            self._notif_handlers[method] = [h for h in self._notif_handlers[method] if h != handler]

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
                    line = await asyncio.wait_for(reader.readline(), timeout=READLINE_TIMEOUT)
                except asyncio.TimeoutError:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
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
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and "method" not in msg:
                    slot = self._pending.pop(int(msg["id"]), None) if isinstance(msg.get("id"), (int, str)) else None
                    if not slot:
                        continue
                    if "error" in msg:
                        slot.fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        slot.fut.set_result(msg.get("result"))
                    continue
                if "method" in msg and "id" in msg:
                    req_id = msg["id"]
                    handler = self._request_handlers.get(msg["method"])
                    if handler:
                        try:
                            result = await handler(msg.get("params") or {})
                            await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
                        except Exception as e:
                            await self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}})
                    else:
                        await self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})
                    continue
                if "method" in msg and "id" not in msg:
                    for h in self._notif_handlers.get(msg["method"], []):
                        try:
                            h(msg.get("params") or {})
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[READER] unexpected error: %s", exc)
        finally:
            for pending in self._pending.values():
                if not pending.fut.done():
                    pending.fut.set_exception(RuntimeError("Process exited unexpectedly"))

    async def _send(self, obj: Dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            return
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _stderr_loop(self) -> None:
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
                    logger.debug("[STDERR] %s", text)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


# ── Codex via App-Server ─────────────────────────────────────────────────────

async def _call_codex(prompt: str, ctx: Optional[Context] = None) -> str:
    """Call Codex CLI via app-server (JSON-RPC 2.0). Per-call subprocess isolation."""
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

        await client.request("initialize", {
            "clientInfo": {"name": "codex-server", "title": "Codex MCP", "version": "0.1.0"},
            "capabilities": {},
        })
        await client.send_notification("initialized")

        thread_result = await client.request("thread/start", {
            "cwd": os.getcwd(),
            "approvalPolicy": "never",
            "sandbox": "workspace-write",
        })
        thread_id = (thread_result or {}).get("thread", {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex app-server: failed to create thread")

        turn_done.clear()
        if ctx:
            await ctx.report_progress(0, 1)
        await client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        })

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
            async with _get_codex_sem():
                result = await _call_codex(prompt, ctx)
            logger.info("[TOOL] ask_codex OK in %.1fs", time.monotonic() - t0)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            errors.append(f"Attempt {attempt + 1}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}")
            logger.warning("[TOOL] ask_codex attempt %d failed in %.1fs: %s", attempt + 1, elapsed, exc)
    return "[ERROR] Codex failed after {} attempts:\n{}".format(MAX_ATTEMPTS, "\n".join(errors))


def main():
    logger.info("codex-server starting")
    mcp.run()


if __name__ == "__main__":
    main()
