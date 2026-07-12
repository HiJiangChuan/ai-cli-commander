"""
kimi agent —— 通过 ACP 协议调用 Kimi CLI。

协议：ACP (Agent Client Protocol) = JSON-RPC 2.0 over NDJSON/stdio
流程：kimi acp → initialize → session/new → session/prompt → 收集 session/update 通知

Kimi CLI 特殊性：它会主动调用 fs/read_text_file、terminal/create 等请求，
我们需要在客户端实现这些处理器，否则 Kimi 会卡住等待响应。

终端模拟的设计：
- 每个终端进程配一个后台排空任务，持续读 stdout 到缓冲区。
  否则命令输出超过 PIPE 缓冲（约 64KB）时会卡住写入，wait_for_exit 永远等不到退出。
- 终端 id 用自增计数器，不用 len()（释放后 len 会回退，导致 id 复用碰撞、进程泄漏）
- stdin=DEVNULL：终端命令绝不继承 MCP 协议管道
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
from typing import Any

from ai_commander.acp import ACPClient
from ai_commander.spawn import is_available, kill_process_tree, require_cli

logger = logging.getLogger(__name__)

NAME = "kimi"
DISPLAY_NAME = "Kimi"
CLI = "kimi"

TIMEOUT = float(os.getenv("KIMI_TIMEOUT", "300"))
HANDSHAKE_TIMEOUT = float(os.getenv("KIMI_HANDSHAKE_TIMEOUT", "30"))
_TERMINAL_WAIT_TIMEOUT = 120.0
_TERMINAL_OUTPUT_CAP = 1024 * 1024  # 单个终端的输出缓冲上限


async def call(prompt: str, *, cwd: str | None = None) -> str:
    require_cli(CLI)

    working_dir = cwd or os.getcwd()
    if not os.path.isdir(working_dir):
        raise RuntimeError(f"工作目录不存在：{working_dir}")

    client = ACPClient([CLI, "acp"])

    # ── Kimi 会发来的请求：我们需要响应，否则 Kimi 会卡住 ────────────

    async def _permission(params: dict) -> dict:
        options = params.get("options") or []
        # 优先选择 allow_always 或 allow_once
        chosen = next((o for o in options if o.get("kind") in ("allow_always", "allow_once")), None)
        if not chosen and options:
            chosen = options[0]
        if not chosen:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    async def _fs_read(params: dict) -> dict:
        path = params.get("path") or params.get("filePath") or ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return {"content": f.read()}
        except Exception:
            return {"content": ""}

    async def _fs_write(params: dict) -> dict:
        path = params.get("path") or params.get("filePath") or ""
        content = params.get("content") or ""
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass
        return {}

    # ── 终端模拟（Kimi 有时会要求执行命令）─────────────────────────

    _terminals: dict[str, dict[str, Any]] = {}
    _terminal_ids = itertools.count(1)

    async def _drain_output(proc: asyncio.subprocess.Process, buf: bytearray) -> None:
        """持续排空终端 stdout，防止 PIPE 写满导致命令永远无法退出。"""
        try:
            assert proc.stdout
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                if len(buf) < _TERMINAL_OUTPUT_CAP:
                    buf.extend(chunk)  # 超出上限后丢弃内容，但继续排空
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _terminal_create(params: dict) -> dict:
        command = params.get("command") or "bash"
        arg_list = [str(a) for a in (params.get("args") or [])]
        term_cwd = params.get("cwd") or working_dir
        common: dict[str, Any] = {
            "cwd": term_cwd,
            "stdin": asyncio.subprocess.DEVNULL,  # 绝不继承 MCP 协议管道
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
            "start_new_session": True,
        }
        if arg_list:
            proc = await asyncio.create_subprocess_exec(command, *arg_list, **common)
        else:
            proc = await asyncio.create_subprocess_shell(command, **common)
        buf = bytearray()
        tid = str(next(_terminal_ids))
        _terminals[tid] = {
            "proc": proc,
            "buf": buf,
            "drain": asyncio.create_task(_drain_output(proc, buf)),
        }
        return {"terminalId": tid}

    async def _terminal_output(params: dict) -> dict:
        term = _terminals.get(params.get("terminalId", ""))
        if not term:
            return {"output": "", "truncated": False}
        result: dict[str, Any] = {
            "output": bytes(term["buf"]).decode("utf-8", errors="replace"),
            "truncated": len(term["buf"]) >= _TERMINAL_OUTPUT_CAP,
        }
        proc = term["proc"]
        if proc.returncode is not None:
            result["exitStatus"] = {"exitCode": proc.returncode}
        return result

    async def _terminal_wait(params: dict) -> dict:
        term = _terminals.get(params.get("terminalId", ""))
        if not term:
            return {"exitCode": -1}
        try:
            code = await asyncio.wait_for(term["proc"].wait(), timeout=_TERMINAL_WAIT_TIMEOUT)
            return {"exitCode": code}
        except TimeoutError:
            return {"exitCode": -1}

    def _release(term: dict[str, Any]) -> None:
        term["drain"].cancel()
        if term["proc"].returncode is None:
            kill_process_tree(term["proc"])

    async def _terminal_release(params: dict) -> dict:
        term = _terminals.pop(params.get("terminalId", ""), None)
        if term:
            _release(term)
        return {}

    client.on_request("session/request_permission", _permission)
    client.on_request("fs/read_text_file", _fs_read)
    client.on_request("fs/write_text_file", _fs_write)
    client.on_request("terminal/create", _terminal_create)
    client.on_request("terminal/output", _terminal_output)
    client.on_request("terminal/wait_for_exit", _terminal_wait)
    client.on_request("terminal/kill", _terminal_release)
    client.on_request("terminal/release", _terminal_release)

    try:
        await client.start()

        # 1. 握手（必须限时：kimi 若在此阶段卡住，工具调用会永久挂起）
        try:
            await client.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "ai-cli-commander", "version": "1.1.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            }, timeout=HANDSHAKE_TIMEOUT)

            # 2. 建立 session
            result = await client.request("session/new", {
                "cwd": working_dir,
                "mcpServers": [],
            }, timeout=HANDSHAKE_TIMEOUT)
        except TimeoutError:
            raise RuntimeError(
                f"Kimi 握手超时（{HANDSHAKE_TIMEOUT:.0f}s）。"
                "请在终端运行 `kimi` 确认 CLI 可正常启动且已登录（`kimi /login`）。"
            ) from None

        session_id = (result or {}).get("sessionId")
        if not session_id:
            raise RuntimeError("Kimi 未返回 sessionId")

        # 3. 收集流式文本。正文和思考分开收集：
        #    正文优先；仅当正文为空时才回退到思考内容（部分场景 kimi 只发 thought）
        message_chunks: list[str] = []
        thought_chunks: list[str] = []

        def _on_update(params: dict[str, Any]) -> None:
            if params.get("sessionId") != session_id:
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate") or update.get("type")
            if kind not in ("agent_message_chunk", "agent_thought_chunk"):
                return
            text = (
                ((update.get("content") or {}).get("text"))
                or update.get("text")
                or ""
            )
            if isinstance(text, str) and text:
                (message_chunks if kind == "agent_message_chunk" else thought_chunks).append(text)

        client.on_notification("session/update", _on_update)
        try:
            await client.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt}],
            }, timeout=TIMEOUT)
        except TimeoutError:
            raise RuntimeError(f"Kimi 超时（{TIMEOUT:.0f}s）") from None
        finally:
            client.off_notification("session/update", _on_update)

        text = "".join(message_chunks).strip() or "".join(thought_chunks).strip()
        if not text:
            raise RuntimeError("Kimi 返回空响应")

        logger.info("kimi OK")
        return text

    finally:
        for term in _terminals.values():
            _release(term)
        _terminals.clear()
        await client.stop()


async def health_check() -> dict:
    try:
        require_cli(CLI)
    except RuntimeError as e:
        return {"agent": NAME, "available": False, "error": str(e)}

    # kimi 健康检查：只验证 CLI 是否存在，不启动完整会话
    return {
        "agent": NAME,
        "available": is_available(CLI),
        "ok": is_available(CLI),
        "note": "仅验证 CLI 存在，完整测试需调用 ask_kimi",
    }
