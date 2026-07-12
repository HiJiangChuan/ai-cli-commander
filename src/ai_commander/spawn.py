"""
subprocess 工具 —— 所有 agent 的统一进程启动层。

设计原则（参考 all-agents-mcp/src/orchestrator/executor.ts）：
- stdin 默认 DEVNULL，彻底切断与 MCP 协议管道的连接
- start_new_session=True：独立进程组，防止 SIGHUP 传播
- 注入终端环境变量，避免 CLI 工具因检测不到 TTY 而挂起
- 超时后 kill 整个进程组（含 CLI 派生的孙进程），不留残留进程
- 日志只记录命令名和参数个数，不记录参数内容（prompt 可能含敏感信息）
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from dataclasses import dataclass

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


def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """杀死子进程及其整个进程组。

    start_new_session=True 保证 pgid == pid，killpg 能覆盖 CLI 派生的孙进程；
    进程组不存在时回退为只杀直接子进程。
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def spawn(
    cmd: str,
    args: list[str],
    *,
    timeout: float = 120.0,
    stdin: str | bytes | None = None,
    env: dict[str, str | None] | None = None,
    cwd: str | None = None,
) -> SpawnResult:
    """
    启动子进程，收集 stdout/stderr，超时后杀死整个进程组。

    stdin=None  → 子进程 stdin 接 DEVNULL（绝不继承 MCP socket）
    stdin=<str> → 通过 pipe 传入，适合 codex exec / claude -p
    env 的值为 None 表示从环境中删除该变量（区别于设为空字符串）
    """
    if cwd and not os.path.isdir(cwd):
        raise RuntimeError(f"工作目录不存在：{cwd}")

    full_env = os.environ.copy()
    full_env.update(_TERMINAL_ENV)
    for key, value in (env or {}).items():
        if value is None:
            full_env.pop(key, None)
        else:
            full_env[key] = value

    stdin_mode = asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL
    stdin_bytes = stdin.encode() if isinstance(stdin, str) else stdin

    t0 = asyncio.get_running_loop().time()

    logger.debug("spawn: %s (%d args)", cmd, len(args))

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
    except TimeoutError:
        timed_out = True
        kill_process_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            pass
        stdout_bytes = b""
        stderr_bytes = b""
    except asyncio.CancelledError:
        # 调用方取消（如 MCP 客户端中断）时同样不能留下残留进程
        kill_process_tree(proc)
        raise

    duration_ms = int((asyncio.get_running_loop().time() - t0) * 1000)

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
