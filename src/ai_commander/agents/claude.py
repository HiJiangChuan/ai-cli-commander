"""
claude agent —— 通过 `claude -p --output-format json` 调用 Claude Code CLI。

关键点：
- 删除 CLAUDECODE 环境变量（置空不等于删除），防止子进程认为自己嵌套在
  Claude Code 内而改变行为
- prompt 通过 stdin 传入（不进 argv：防止 ps 泄露、绕开 ARG_MAX 限制）
- 不使用 --dangerously-skip-permissions：--allowedTools 白名单在非交互
  模式下才真正生效（白名单外的工具会被自动拒绝）
- stdin/stdout 由 spawn 层隔离，解析 stdout JSON 提取 result 字段
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


def _parse_response(stdout: str) -> dict:
    """解析 claude -p --output-format json 的输出，容忍 JSON 前后的杂音。"""
    data = None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

    if not isinstance(data, dict):
        raise RuntimeError(f"Claude 返回无效 JSON：{stdout[:300]}")
    return data


async def call(
    prompt: str,
    *,
    max_turns: int = 0,
    allowed_tools: str = "Read,Bash,Edit",
    cwd: str | None = None,
) -> str:
    require_cli(CLI)

    effective_turns = max_turns or MAX_TURNS

    args = [
        "-p",  # print 模式；prompt 经 stdin 传入
        "--output-format", "json",
        "--max-turns", str(effective_turns),
    ]
    if MODEL:
        args += ["--model", MODEL]
    if allowed_tools:
        args += ["--allowedTools", allowed_tools]

    # None = 从环境中删除，防止子 claude 认为自己嵌套在 Claude Code 内
    env: dict[str, str | None] = {"CLAUDECODE": None}

    result = await spawn(CLI, args, stdin=prompt, timeout=TIMEOUT, env=env, cwd=cwd)

    if result.timed_out:
        raise RuntimeError(f"Claude 超时（{TIMEOUT:.0f}s）")

    stderr = result.stderr.strip()
    if "not logged in" in stderr.lower():
        raise RuntimeError("Claude 未登录。请在终端执行 `claude /login` 后重试。")

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"Claude 返回空 stdout。stderr: {stderr[:300]}")

    data = _parse_response(stdout)

    if data.get("is_error"):
        raise RuntimeError(f"Claude 报错：{data.get('result') or data}")
    if data.get("error"):
        raise RuntimeError(f"Claude 报错：{data['error']}")

    text = str(data.get("result") or data.get("response") or "").strip()
    if not text and result.exit_code != 0:
        raise RuntimeError(f"Claude 退出码 {result.exit_code}，无响应内容")

    cost = data.get("total_cost_usd") or data.get("cost_usd")
    logger.info("claude OK cost=$%s %dms", cost, result.duration_ms)
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
