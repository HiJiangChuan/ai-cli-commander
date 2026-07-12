"""
ACPClient —— JSON-RPC 2.0 over NDJSON/stdio 客户端。

用于需要双向握手协议的 CLI（目前仅 kimi acp）。要点：

- stdout/stderr 的 StreamReader 上限设为 10MB：NDJSON 一条消息一行，
  asyncio 默认 64KB 行上限会被大消息（如工具结果）击穿，导致 reader 崩溃
- request() 支持超时，超时后清理 pending，防止握手阶段永久挂起
- 来自子进程的请求由独立 task 处理，不阻塞 reader loop
  （否则一个耗时 handler 会卡住所有响应和通知的分发）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

NotifHandler = Callable[[dict[str, Any]], None]
RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# NDJSON 单行上限。默认 64KB 会被大消息击穿。
_STREAM_LIMIT = 10 * 1024 * 1024


class ACPClient:
    """最小化 JSON-RPC 2.0 客户端，通过子进程 stdio 通信。"""

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None):
        self._cmd = cmd
        self._env: dict[str, str] = {**os.environ, **(env or {})}
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._notif_handlers: dict[str, list[NotifHandler]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._handler_tasks: set[asyncio.Task] = set()

    # ── 生命周期 ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            start_new_session=True,
            limit=_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._reader_loop(), name="acp-reader")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="acp-stderr")

    async def stop(self) -> None:
        # 取消所有待处理请求
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        # 终止子进程
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except Exception:
                    pass
        self._proc = None

        # 取消后台任务（reader / stderr / 未完成的请求 handler）
        tasks = [
            t
            for t in (self._reader_task, self._stderr_task, *self._handler_tasks)
            if t and not t.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        self._handler_tasks.clear()

    # ── 处理器注册 ───────────────────────────────────────────────────────

    def on_notification(self, method: str, handler: NotifHandler) -> None:
        self._notif_handlers.setdefault(method, []).append(handler)

    def off_notification(self, method: str, handler: NotifHandler | None = None) -> None:
        if handler is None:
            self._notif_handlers.pop(method, None)
        else:
            handlers = self._notif_handlers.get(method, [])
            self._notif_handlers[method] = [h for h in handlers if h != handler]

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    # ── 发送 ─────────────────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACPClient 未启动")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
            if timeout is not None:
                return await asyncio.wait_for(fut, timeout)
            return await fut
        finally:
            self._pending.pop(msg_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACPClient 未启动")
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        await self._write(msg)

    # ── 内部 ─────────────────────────────────────────────────────────────

    async def _write(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        data = (json.dumps(obj) + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _handle_peer_request(self, msg_id: Any, method: str, params: dict) -> None:
        """处理来自子进程的请求。独立 task 运行，不阻塞 reader loop。"""
        handler = self._request_handlers.get(method)
        try:
            if handler is None:
                await self._write({"jsonrpc": "2.0", "id": msg_id,
                                   "error": {"code": -32601, "message": "Method not found"}})
                return
            result = await handler(params)
            await self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                await self._write({"jsonrpc": "2.0", "id": msg_id,
                                   "error": {"code": -32000, "message": str(e)}})
            except Exception:
                pass

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
                    try:
                        key = int(msg_id)
                    except (TypeError, ValueError):
                        continue
                    fut = self._pending.pop(key, None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result"))
                    continue

                # 来自子进程的请求（需要回复）—— 交给独立 task，防止阻塞 reader
                if msg_id is not None and method is not None:
                    task = asyncio.create_task(
                        self._handle_peer_request(msg_id, method, msg.get("params") or {}),
                        name=f"acp-handler-{method}",
                    )
                    self._handler_tasks.add(task)
                    task.add_done_callback(self._handler_tasks.discard)
                    continue

                # 通知
                if msg_id is None and method is not None:
                    for h in self._notif_handlers.get(method, []):
                        try:
                            h(msg.get("params") or {})
                        except Exception:
                            logger.debug("ACP notification handler error", exc_info=True)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("ACP reader error: %s", exc)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("ACP 进程意外退出"))
            self._pending.clear()

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
