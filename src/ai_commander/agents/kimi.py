"""
kimi agent —— 通过 ACP 协议调用 Kimi CLI。

协议：ACP (Agent Client Protocol) = JSON-RPC 2.0 over NDJSON/stdio
流程：kimi acp → initialize → session/new → session/prompt → 收集 session/update 通知

Kimi CLI 特殊性：它会主动调用 fs/read_text_file、terminal/create 等请求，
我们需要在客户端实现这些处理器，否则 Kimi 会卡住等待响应。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ai_commander.acp import ACPClient
from ai_commander.spawn import is_available, require_cli

logger = logging.getLogger(__name__)

NAME = "kimi"
DISPLAY_NAME = "Kimi"
CLI = "kimi"

TIMEOUT = float(os.getenv("KIMI_TIMEOUT", "300"))


async def call(prompt: str) -> str:
    require_cli(CLI)

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

    # 简单的终端模拟（Kimi 有时会要求执行命令）
    _terminals: dict[str, asyncio.subprocess.Process] = {}

    async def _terminal_create(params: dict) -> dict:
        cmd = params.get("command") or "bash"
        cwd = params.get("cwd") or os.getcwd()
        tid = str(len(_terminals) + 1)
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        _terminals[tid] = proc
        return {"terminalId": tid}

    async def _terminal_output(params: dict) -> dict:
        proc = _terminals.get(params.get("terminalId", ""))
        if not proc or not proc.stdout:
            return {"output": ""}
        try:
            data = await asyncio.wait_for(proc.stdout.read(65536), timeout=5.0)
            return {"output": data.decode("utf-8", errors="replace")}
        except asyncio.TimeoutError:
            return {"output": ""}

    async def _terminal_wait(params: dict) -> dict:
        proc = _terminals.get(params.get("terminalId", ""))
        if not proc:
            return {"exitCode": -1}
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=60.0)
            return {"exitCode": code}
        except asyncio.TimeoutError:
            return {"exitCode": -1}

    async def _terminal_release(params: dict) -> dict:
        proc = _terminals.pop(params.get("terminalId", ""), None)
        if proc and proc.returncode is None:
            proc.kill()
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

        # 1. 握手
        await client.request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "ai-commander", "version": "1.0.0"},
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
        })

        # 2. 建立 session
        result = await client.request("session/new", {
            "cwd": os.getcwd(),
            "mcpServers": [],
        })
        session_id = (result or {}).get("sessionId")
        if not session_id:
            raise RuntimeError("Kimi 未返回 sessionId")

        # 3. 收集流式文本
        chunks: list[str] = []

        def _on_update(params: dict[str, Any]) -> None:
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
                    chunks.append(text)

        client.on_notification("session/update", _on_update)
        try:
            await asyncio.wait_for(
                client.request("session/prompt", {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                }),
                timeout=TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Kimi 超时（{TIMEOUT:.0f}s）")
        finally:
            client.off_notification("session/update", _on_update)

        text = "".join(chunks).strip()
        if not text:
            raise RuntimeError("Kimi 返回空响应")

        logger.info("kimi OK")
        return text

    finally:
        for proc in _terminals.values():
            if proc.returncode is None:
                proc.kill()
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
        "note": "kimi 健康检查仅验证 CLI 存在，完整测试需要调用 ask_kimi",
    }
