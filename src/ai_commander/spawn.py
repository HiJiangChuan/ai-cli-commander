"""
subprocess 工具 —— 所有 agent 的统一进程启动层。

设计原则（参考 all-agents-mcp/src/orchestrator/executor.ts）：
- stdin 默认 DEVNULL，彻底切断与 MCP 协议管道的连接
- start_new_session=True：独立进程组，防止 SIGHUP 传播
- 注入终端环境变量，避免 CLI 工具因检测不到 TTY 而挂起
- 超时后强制 kill，不留僵尸进程
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# 注入给所有子进程的基础环境，模拟终端行为
_TERMINAL_ENV = {
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
}


@dataclass
class SpawnResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0


def is_available(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def require_cli(cmd: str) -> None:
    if not is_available(cmd):
        raise RuntimeError(f"'{cmd}' 未安装或不在 PATH 中，请先安装。")


async def spawn(
    cmd: str,
    args: list[str],
    *,
    timeout: float = 120.0,
    stdin: Optional[str | bytes] = None,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> SpawnResult:
    """
    启动子进程，收集 stdout/stderr，超时后强制终止。

    stdin=None  → 子进程 stdin 接 DEVNULL（绝不继承 MCP socket）
    stdin=<str> → 通过 pipe 传入，适合 codex exec / kimi acp
    """
    full_env = os.environ.copy()
    full_env.update(_TERMINAL_ENV)
    if env:
        full_env.update(env)

    stdin_mode = asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL
    stdin_bytes = stdin.encode() if isinstance(stdin, str) else stdin

    t0 = asyncio.get_event_loop().time()

    logger.debug("spawn: %s %s", cmd, " ".join(args))

    proc = await asyncio.create_subprocess_exec(
        cmd,
        *args,
        stdin=stdin_mode,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
        cwd=cwd,
        start_new_session=True,  # 独立进程组，防止 SIGHUP
    )

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(stdin_bytes),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            pass
        stdout_bytes = b""
        stderr_bytes = b""

    duration_ms = int((asyncio.get_event_loop().time() - t0) * 1000)

    result = SpawnResult(
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )

    if timed_out:
        logger.warning("spawn timeout: %s (%.0fs)", cmd, timeout)
    else:
        logger.debug("spawn done: %s exit=%d %dms", cmd, result.exit_code, duration_ms)

    return result
