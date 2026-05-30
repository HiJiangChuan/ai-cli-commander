"""
ai-commander core — shared utilities for all MCP servers.

Public API:
    setup_logging(name) -> logging.Logger
    ACPClient(cmd, env=None, logger=None)
    check_cli(name) -> bool
    ensure_cli(name) -> None
    get_semaphore(name, max_concurrent) -> asyncio.Semaphore
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

LOG_DIR = os.path.expanduser(os.getenv("LOG_DIR", "~/.ai-commander/logs"))

_semaphores: Dict[str, asyncio.Semaphore] = {}


# ── Logging ─────────────────────────────────────────────────────────────────


def setup_logging(name: str) -> logging.Logger:
    """Configure and return a logger with a single file handler.

    Safe to call multiple times — idempotent.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    os.makedirs(LOG_DIR, exist_ok=True)
    fh = logging.FileHandler(os.path.join(LOG_DIR, f"{name}.log"))
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    logger.addHandler(fh)
    return logger


# ── Semaphore factory ───────────────────────────────────────────────────────


def get_semaphore(name: str, max_concurrent: int) -> asyncio.Semaphore:
    """Return a named, lazily-created asyncio.Semaphore."""
    if name not in _semaphores:
        _semaphores[name] = asyncio.Semaphore(max_concurrent)
    return _semaphores[name]


# ── CLI availability ────────────────────────────────────────────────────────


def check_cli(name: str) -> bool:
    return shutil.which(name) is not None


def ensure_cli(name: str) -> None:
    if not check_cli(name):
        raise RuntimeError(f"'{name}' CLI not found on PATH. Install it first.")


# ── LineBuffer ──────────────────────────────────────────────────────────────


class LineBuffer:
    """Async line reader that handles arbitrary line lengths."""

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


# ── ACPClient ───────────────────────────────────────────────────────────────


@dataclass
class _Pending:
    fut: asyncio.Future


class ACPClient:
    """Minimal JSON-RPC 2.0 client over newline-delimited JSON on stdio."""

    def __init__(
        self,
        cmd: List[str],
        env: Optional[Dict[str, str]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._cmd = cmd
        self._env = env or os.environ.copy()
        self._logger = logger or logging.getLogger("ai_commander.acp")
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: Dict[int, _Pending] = {}
        self._notif_handlers: Dict[str, List[Callable]] = {}
        self._request_handlers: Dict[
            str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
        ] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        # FIX: restart if the previous process has already exited.
        if self._proc is not None and self._proc.returncode is None:
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

    def off_notification(
        self, method: str, handler: Optional[Callable] = None
    ) -> None:
        if method not in self._notif_handlers:
            return
        if handler is None:
            self._notif_handlers[method] = []
        else:
            self._notif_handlers[method] = [
                h for h in self._notif_handlers[method] if h != handler
            ]

    def on_request(
        self,
        method: str,
        handler: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
    ) -> None:
        self._request_handlers[method] = handler

    async def request(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
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

    async def send_notification(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACP process not started")
        obj = {"jsonrpc": "2.0", "method": method}
        if params:
            obj["params"] = params
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    # ── internals ─────────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        consecutive_timeouts = 0
        readline_timeout = int(os.getenv("READLINE_TIMEOUT", "120"))
        max_consecutive = int(os.getenv("MAX_CONSECUTIVE_TIMEOUTS", "3"))
        try:
            if not self._proc or not self._proc.stdout:
                return
            reader = LineBuffer(self._proc.stdout)
            while True:
                try:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=readline_timeout
                    )
                except asyncio.TimeoutError:
                    consecutive_timeouts += 1
                    self._logger.warning(
                        "[READER] readline timeout (%d/%d)",
                        consecutive_timeouts,
                        max_consecutive,
                    )
                    if consecutive_timeouts >= max_consecutive:
                        self._logger.error(
                            "[READER] %d consecutive timeouts, killing process",
                            max_consecutive,
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
                    self._logger.debug("[READER] non-JSON line: %s", line[:200])
                    continue

                if not isinstance(msg, dict):
                    continue

                # Response to our request
                if "id" in msg and "method" not in msg:
                    slot = (
                        self._pending.pop(int(msg["id"]), None)
                        if isinstance(msg.get("id"), (int, str))
                        else None
                    )
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
                            await self._send(
                                {"jsonrpc": "2.0", "id": req_id, "result": result}
                            )
                        except Exception as e:
                            await self._send(
                                {
                                    "jsonrpc": "2.0",
                                    "id": req_id,
                                    "error": {"code": -32000, "message": str(e)},
                                }
                            )
                    else:
                        await self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "error": {
                                    "code": -32601,
                                    "message": "Method not found",
                                },
                            }
                        )
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
            self._logger.error("[READER] unexpected error: %s", exc)
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
                    self._logger.debug("[STDERR] %s", text)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
