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
    """Call Kimi CLI via ACP protocol (standard ACP: session/new + session/prompt)."""
    _ensure_cli("kimi")
    client = _ACPClient(["kimi", "acp"])

    # ── Client-side request handlers (Kimi CLI calls us) ────────────────

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

    _terminals: Dict[str, asyncio.subprocess.Process] = {}

    async def _terminal_create(params: Dict[str, Any]) -> Dict[str, Any]:
        cmd = params.get("command") or params.get("cmd") or "bash"
        cwd = params.get("cwd") or os.getcwd()
        terminal_id = str(len(_terminals) + 1)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            _terminals[terminal_id] = proc
        except Exception as e:
            logger.warning("[KIMI] terminal_create failed: %s", e)
        return {"terminalId": terminal_id}

    async def _terminal_output(params: Dict[str, Any]) -> Dict[str, Any]:
        proc = _terminals.get(params.get("terminalId", ""))
        if not proc or not proc.stdout:
            return {"output": ""}
        try:
            data = await asyncio.wait_for(proc.stdout.read(65536), timeout=5.0)
            return {"output": data.decode("utf-8", errors="replace")}
        except asyncio.TimeoutError:
            return {"output": ""}

    async def _terminal_wait(params: Dict[str, Any]) -> Dict[str, Any]:
        proc = _terminals.get(params.get("terminalId", ""))
        if not proc:
            return {"exitCode": -1}
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=60.0)
            return {"exitCode": code}
        except asyncio.TimeoutError:
            return {"exitCode": -1}

    async def _terminal_kill(params: Dict[str, Any]) -> Dict[str, Any]:
        proc = _terminals.pop(params.get("terminalId", ""), None)
        if proc and proc.returncode is None:
            proc.kill()
        return {}

    async def _terminal_release(params: Dict[str, Any]) -> Dict[str, Any]:
        proc = _terminals.pop(params.get("terminalId", ""), None)
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
        logger.info("[KIMI] process started, initializing ACP")

        # Step 1: ACP initialize handshake
        await client.request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "kimi-server", "version": "0.1.0"},
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
        })

        # Step 2: Create session
        result = await client.request("session/new", {"cwd": os.getcwd(), "mcpServers": []})
        session_id = result.get("sessionId")
        if not session_id:
            raise RuntimeError("Failed to create Kimi session")

        # Step 3: Collect streaming text via session/update notifications
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

            # Step 4: Send prompt
            try:
                await asyncio.wait_for(
                    client.request("session/prompt", {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    }),
                    timeout=KIMI_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Kimi timed out after {KIMI_TIMEOUT}s")

            if ctx:
                await ctx.report_progress(1, 1)
            return "".join(text_chunks).strip() or "[No response from Kimi]"
        finally:
            client.off_notification("session/update", _on_update)
    finally:
        for proc in _terminals.values():
            if proc.returncode is None:
                proc.kill()
        _terminals.clear()
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
