"""
agy agent —— 通过 `agy --print` 调用 Antigravity CLI。

要点：
1. stdin=DEVNULL：切断与 MCP 协议管道的连接
2. start_new_session=True：独立进程组，防止 SIGHUP
3. 注入终端环境变量：让 agy 认为自己在终端中运行
4. 不重试超时（重试只会堆积更多残留 agy 进程）

注意：prompt 经 argv 传入（agy --print 的接口如此），在多用户系统上
对 `ps` 可见，且受 ARG_MAX 限制；超长 prompt 请拆分。
"""
from __future__ import annotations

import logging
import os

from ai_commander.spawn import SpawnResult, require_cli, spawn

logger = logging.getLogger(__name__)

NAME = "agy"
DISPLAY_NAME = "Antigravity (agy)"
CLI = "agy"

TIMEOUT = float(os.getenv("AGY_TIMEOUT", "180"))
MODEL = os.getenv("AGY_MODEL", "")  # 留空则使用 agy 默认模型


async def call(prompt: str, *, cwd: str | None = None) -> str:
    require_cli(CLI)

    args = ["--print", prompt, "--dangerously-skip-permissions"]
    if MODEL:
        args = ["--model", MODEL] + args

    result: SpawnResult = await spawn(CLI, args, timeout=TIMEOUT, cwd=cwd)

    if result.timed_out:
        raise RuntimeError(
            f"agy 超时（{TIMEOUT:.0f}s）。"
            "请确认 agy 守护进程正在运行（在终端执行一次 `agy` 即可启动守护进程）。"
        )

    if result.exit_code != 0:
        err = result.stderr.strip()[:500]
        raise RuntimeError(f"agy 退出码 {result.exit_code}：{err or '（无错误信息）'}")

    text = result.stdout.strip()
    if not text:
        raise RuntimeError("agy 返回空响应")

    logger.info("agy OK %dms", result.duration_ms)
    return text


async def health_check() -> dict:
    """检查 agy 是否可用（30 秒超时，会真实调用一次模型）。"""
    try:
        require_cli(CLI)
    except RuntimeError as e:
        return {"agent": NAME, "available": False, "error": str(e)}

    result = await spawn(CLI, ["--print", "Reply with: OK", "--dangerously-skip-permissions"],
                         timeout=30.0)
    return {
        "agent": NAME,
        "available": True,
        "ok": result.ok,
        "latency_ms": result.duration_ms,
        "error": (
            "timeout after 30s" if result.timed_out
            else (result.stderr.strip()[:200] if result.exit_code != 0 else None)
        ),
    }
