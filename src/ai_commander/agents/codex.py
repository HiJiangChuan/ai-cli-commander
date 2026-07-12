"""
codex agent —— 通过 `codex exec` 调用 Codex CLI。

架构说明：
  旧版：app-server JSON-RPC 协议（复杂、易碎、需要握手）
  新版：codex exec --output-last-message=<tmpfile> -
        prompt 通过 stdin 传入，结果从临时文件读取
        参考：all-agents-mcp/src/agents/codex-agent.ts

优势：
- 无需理解 JSON-RPC 协议细节
- codex 版本升级不会破坏兼容性
- 错误处理更直接
"""
from __future__ import annotations

import logging
import os
import tempfile
import uuid

from ai_commander.spawn import require_cli, spawn

logger = logging.getLogger(__name__)

NAME = "codex"
DISPLAY_NAME = "Codex"
CLI = "codex"

TIMEOUT = float(os.getenv("CODEX_TIMEOUT", "300"))
MODEL = os.getenv("CODEX_MODEL", "")
REASONING = os.getenv("CODEX_REASONING", "medium")

_REASONING_LEVELS = {"minimal", "low", "medium", "high", "xhigh"}


async def call(
    prompt: str,
    *,
    model: str = "",
    reasoning: str = "",
    cwd: str | None = None,
) -> str:
    require_cli(CLI)

    effective_model = model or MODEL
    effective_reasoning = (reasoning or REASONING).strip().lower()
    if effective_reasoning and effective_reasoning not in _REASONING_LEVELS:
        raise ValueError(
            f"reasoning 必须是 {'/'.join(sorted(_REASONING_LEVELS))} 之一，收到：{reasoning!r}"
        )

    # 用临时文件接收输出，避免解析 codex 的 stdout 进度信息
    output_file = os.path.join(tempfile.gettempdir(), f"codex-out-{uuid.uuid4().hex}.txt")

    args = [
        "exec",
        "--sandbox", "read-only",
        "--skip-git-repo-check",  # MCP server 的 cwd 不一定是 git 仓库
        f"--output-last-message={output_file}",
    ]
    if effective_model:
        args += ["--model", effective_model]
    if effective_reasoning:
        args += ["-c", f"model_reasoning_effort={effective_reasoning}"]
    args.append("-")  # 从 stdin 读取 prompt

    try:
        result = await spawn(CLI, args, stdin=prompt, timeout=TIMEOUT, cwd=cwd)
    finally:
        # 读取并清理临时文件
        content = ""
        try:
            if os.path.exists(output_file):
                with open(output_file, encoding="utf-8") as f:
                    content = f.read().strip()
                os.unlink(output_file)
        except Exception:
            pass

    if result.timed_out:
        raise RuntimeError(f"Codex 超时（{TIMEOUT:.0f}s）")

    if result.exit_code != 0 and not content:
        err = result.stderr.strip()[:500]
        raise RuntimeError(f"Codex 退出码 {result.exit_code}：{err or '（无错误信息）'}")

    text = content or result.stdout.strip()
    if not text:
        raise RuntimeError("Codex 返回空响应")

    logger.info("codex OK %dms", result.duration_ms)
    return text


async def health_check() -> dict:
    try:
        require_cli(CLI)
    except RuntimeError as e:
        return {"agent": NAME, "available": False, "error": str(e)}

    result = await spawn(CLI, ["--version"], timeout=10.0)
    return {
        "agent": NAME,
        "available": True,
        "ok": result.ok,
        "latency_ms": result.duration_ms,
        "error": result.stderr.strip()[:200] if result.exit_code != 0 else None,
    }
