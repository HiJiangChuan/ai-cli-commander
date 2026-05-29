"""
kimi-server: MCP server bridging agents to Kimi CLI via ACP protocol.

Protocol: ACP (Agent Client Protocol) — JSON-RPC 2.0 over NDJSON/stdio.
Per-call subprocess isolation — each tool call spawns a fresh kimi process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP

# ── Logging ─────────────────────────────────────────────────────────────────

LOG_DIR = os.path.expanduser(os.getenv("LOG_DIR", "~/.ai-commander/logs"))
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("kimi-server")
logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(os.path.join(LOG_DIR, "kimi-server.log"))
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_file_handler)

# ── Configuration ────────────────────────────────────────────────────────────

KIMI_TIMEOUT = int(os.getenv("KIMI_TIMEOUT", "300"))
KIMI_MAX_CONCURRENT = int(os.getenv("KIMI_MAX_CONCURRENT", "4"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
READLINE_TIMEOUT = int(os.getenv("READLINE_TIMEOUT", "120"))
MAX_CONSECUTIVE_TIMEOUTS = int(os.getenv("MAX_CONSECUTIVE_TIMEOUTS", "3"))

_kimi_sem: Optional[asyncio.Semaphore] = None


def _get_kimi_sem() -> asyncio.Semaphore:
    global _kimi_sem
    if _kimi_sem is None:
        _kimi_sem = asyncio.Semaphore(KIMI_MAX_CONCURRENT)
    return _kimi_sem


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
                    logger.warning("[READER] readline timeout (%d/%d)", consecutive_timeouts, MAX_CONSECUTIVE_TIMEOUTS)
                    if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                        logger.error("[READER] %d consecutive timeouts, killing process", MAX_CONSECUTIVE_TIMEOUTS)
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
                    logger.debug("[READER] non-JSON line: %s", line[:200])
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


# ── Kimi via ACP ─────────────────────────────────────────────────────────────

async def _call_kimi(prompt: str, ctx: Optional[Context] = None) -> str:
    """Call Kimi CLI via ACP protocol. Per-call subprocess isolation."""
    _ensure_cli("kimi")
    client = _ACPClient(["kimi", "acp"])

    # Collect streaming response text via notification handlers
    text_chunks: List[str] = []
    chunk_count = 0
    response_done = asyncio.Event()
    response_error: List[str] = []

    def _on_update(params: Dict[str, Any]) -> None:
        nonlocal chunk_count
        # Kimi ACP may stream content via various notification types;
        # we collect from the most common ones.
        content = params.get("content") or params.get("delta") or params.get("text") or ""
        if isinstance(content, str) and content:
            text_chunks.append(content)
            chunk_count += 1
        # Some ACP implementations signal completion via a 'done' field
        if params.get("done") or params.get("finished"):
            response_done.set()

    def _on_error(params: Dict[str, Any]) -> None:
        error_msg = params.get("message") or params.get("error") or "Unknown error"
        response_error.append(str(error_msg))
        response_done.set()

    client.on_notification("agent/update", _on_update)
    client.on_notification("agent/delta", _on_update)
    client.on_notification("agent/error", _on_error)

    try:
        await client.start()
        logger.info("[KIMI] process started, initializing ACP")

        # Initialize handshake
        await client.request("initialize", {
            "clientInfo": {"name": "kimi-server", "version": "0.1.0"},
            "capabilities": {},
        })

        if ctx:
            await ctx.report_progress(0, 1)

        # Send prompt via agent/request
        session_id = str(uuid.uuid4())
        try:
            result = await asyncio.wait_for(
                client.request("agent/request", {
                    "session_id": session_id,
                    "work_dir": os.getcwd(),
                    "prompt": prompt,
                    "yolo": True,
                }),
                timeout=KIMI_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Kimi timed out after {KIMI_TIMEOUT}s")

        # If result comes back directly, use it; otherwise wait for notifications
        if result and isinstance(result, dict):
            direct_content = result.get("content") or ""
            if direct_content:
                return str(direct_content).strip()

        # Fallback: if the response was streamed via notifications
        if not response_done.is_set():
            try:
                await asyncio.wait_for(response_done.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass  # notifications may not have a done signal

        if response_error:
            raise RuntimeError(f"Kimi error: {response_error[0]}")

        if ctx:
            await ctx.report_progress(1, 1)
        return "".join(text_chunks).strip() or "[No response from Kimi]"
    finally:
        await client.stop()


# ── FastMCP Server ───────────────────────────────────────────────────────────

mcp = FastMCP("kimi-server")


@mcp.tool()
async def ask_kimi(prompt: str, ctx: Context) -> str:
    """Send a prompt to Kimi (Kimi K2.5) and return its response."""
    errors: List[str] = []
    for attempt in range(MAX_ATTEMPTS):
        t0 = time.monotonic()
        try:
            async with _get_kimi_sem():
                result = await _call_kimi(prompt, ctx)
            logger.info("[TOOL] ask_kimi OK in %.1fs", time.monotonic() - t0)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            errors.append(f"Attempt {attempt + 1}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}")
            logger.warning("[TOOL] ask_kimi attempt %d failed in %.1fs: %s", attempt + 1, elapsed, exc)
    return "[ERROR] Kimi failed after {} attempts:\n{}".format(MAX_ATTEMPTS, "\n".join(errors))


def main():
    logger.info("kimi-server starting")
    mcp.run()


if __name__ == "__main__":
    main()
