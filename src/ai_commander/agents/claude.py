"""
claude agent —— 通过 `claude -p --output-format json` 调用 Claude Code CLI。

关键点：
- 必须设置 CLAUDECODE="" 环境变量，防止递归调用（Claude 调 Claude）
- stdin=DEVNULL + start_new_session=True 隔离进程
- 解析 stdout JSON，提取 result 字段
"""
from __future__ import annotations

import json
import logging
import os

from ai_commander.spawn import require_cli, spawn

logger = logging.getLogger(__name__)

NAME = "claude"
DISPLAY_NAME = "Claude Code"
CLI = "claude"

TIMEOUT = float(os.getenv("CLAUDE_TIMEOUT", "300"))
MODEL = os.getenv("CLAUDE_MODEL", "")
MAX_TURNS = int(os.getenv("CLAUDE_MAX_TURNS", "10"))


async def call(
    prompt: str,
    *,
    max_turns: int = 0,
    allowed_tools: str = "Read,Bash,Edit",
) -> str:
    require_cli(CLI)

    effective_turns = max_turns or MAX_TURNS

    args = [
        "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--max-turns", str(effective_turns),
    ]
    if MODEL:
        args += ["--model", MODEL]
    if allowed_tools:
        args += ["--allowedTools", allowed_tools]

    env = {
        # 防止递归：告知子进程它是 Claude 调用的
        "CLAUDECODE": "",
    }

    result = await spawn(CLI, args, timeout=TIMEOUT, env=env)

    if result.timed_out:
        raise RuntimeError(f"Claude 超时（{TIMEOUT:.0f}s）")

    stderr = result.stderr.strip()
    if "not logged in" in stderr.lower():
        raise RuntimeError(
            "Claude 未登录。请在终端执行 `claude /login` 后重试。"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"Claude 返回空 stdout。stderr: {stderr[:300]}")

    # 解析 JSON 输出
    data = None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # 有时 Claude 在 JSON 前后输出其他内容，尝试提取
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

    if data is None:
        raise RuntimeError(f"Claude 返回无效 JSON：{stdout[:300]}")

    if data.get("error"):
        raise RuntimeError(f"Claude 报错：{data['error']}")

    text = str(data.get("result") or data.get("response") or "").strip()
    if not text and result.exit_code != 0:
        raise RuntimeError(f"Claude 退出码 {result.exit_code}，无响应内容")

    logger.info("claude OK cost=$%s %dms", data.get("cost_usd"), result.duration_ms)
    return text or "[Claude 返回空响应]"


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
