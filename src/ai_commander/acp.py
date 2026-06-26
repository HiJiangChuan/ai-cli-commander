"""
ACPClient —— JSON-RPC 2.0 over NDJSON/stdio 客户端。

用于需要双向握手协议的 CLI（目前仅 kimi acp）。
从 core.py 提取并重写，修复了以下问题：
- reader_loop 使用 asyncio.StreamReader 原生 API，不再需要 LineBuffer
- stop() 更健壮，不会在 cancel 时死锁
- 通知/请求 handler 注册接口更清晰
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

NotifHandler = Callable[[dict[str, Any]], None]
RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class _Pending:
    fut: asyncio.Future


class ACPClient:
    """最小化 JSON-RPC 2.0 客户端，通过子进程 stdio 通信。"""

    def __init__(self, cmd: list[str], env: Optional[dict[str, str]] = None):
        self._cmd = cmd
        self._env: dict[str, str] = {**os.environ, **(env or {})}
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._notif_handlers: dict[str, list[NotifHandler]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

    # ── 生命周期 ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            start_new_session=True,
        )
        self._reader_task = asyncio.create_task(self._reader_loop(), name="acp-reader")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="acp-stderr")

    async def stop(self) -> None:
        # 取消所有待处理请求
        for p in self._pending.values():
            if not p.fut.done():
                p.fut.cancel()
        self._pending.clear()

        # 终止子进程
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except Exception:
                    pass
        self._proc = None

        # 取消后台任务
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_task = None
        self._stderr_task = None

    # ── 处理器注册 ───────────────────────────────────────────────────────

    def on_notification(self, method: str, handler: NotifHandler) -> None:
        self._notif_handlers.setdefault(method, []).append(handler)

    def off_notification(self, method: str, handler: Optional[NotifHandler] = None) -> None:
        if handler is None:
            self._notif_handlers.pop(method, None)
        else:
            handlers = self._notif_handlers.get(method, [])
            self._notif_handlers[method] = [h for h in handlers if h != handler]

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    # ── 发送 ─────────────────────────────────────────────────────────────

    async def request(self, method: str, params: Optional[dict] = None) -> Any:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACPClient 未启动")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = _Pending(fut=fut)
        await self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        return await fut

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACPClient 未启动")
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        await self._write(msg)

    # ── 内部 ─────────────────────────────────────────────────────────────

    async def _write(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        data = (json.dumps(obj) + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _reader_loop(self) -> None:
        try:
            assert self._proc and self._proc.stdout
            reader = self._proc.stdout
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    logger.debug("ACP non-JSON: %s", line[:200])
                    continue

                if not isinstance(msg, dict):
                    continue

                msg_id = msg.get("id")
                method = msg.get("method")

                # 响应
                if msg_id is not None and method is None:
                    slot = self._pending.pop(int(msg_id), None)
                    if slot and not slot.fut.done():
                        if "error" in msg:
                            slot.fut.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            slot.fut.set_result(msg.get("result"))
                    continue

                # 来自子进程的请求（需要回复）
                if msg_id is not None and method is not None:
                    handler = self._request_handlers.get(method)
                    if handler:
                        try:
                            result = await handler(msg.get("params") or {})
                            await self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})
                        except Exception as e:
                            await self._write({"jsonrpc": "2.0", "id": msg_id,
                                               "error": {"code": -32000, "message": str(e)}})
                    else:
                        await self._write({"jsonrpc": "2.0", "id": msg_id,
                                           "error": {"code": -32601, "message": "Method not found"}})
                    continue

                # 通知
                if msg_id is None and method is not None:
                    for h in self._notif_handlers.get(method, []):
                        try:
                            h(msg.get("params") or {})
                        except Exception:
                            pass

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("ACP reader error: %s", exc)
        finally:
            for p in self._pending.values():
                if not p.fut.done():
                    p.fut.set_exception(RuntimeError("ACP 进程意外退出"))

    async def _stderr_loop(self) -> None:
        try:
            assert self._proc and self._proc.stderr
            reader = self._proc.stderr
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("ACP stderr: %s", text)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
