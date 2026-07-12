"""spawn 层测试：基础执行、stdin、env 增删、超时进程组清理。"""
from __future__ import annotations

import asyncio
import os

import pytest

from ai_commander.spawn import is_available, require_cli, spawn


async def test_basic_echo():
    result = await spawn("sh", ["-c", "echo hello"], timeout=10)
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.exit_code == 0
    assert not result.timed_out


async def test_stdin_pipe():
    result = await spawn("cat", [], stdin="ping", timeout=10)
    assert result.ok
    assert result.stdout == "ping"


async def test_exit_code():
    result = await spawn("sh", ["-c", "exit 3"], timeout=10)
    assert not result.ok
    assert result.exit_code == 3


async def test_stderr_captured():
    result = await spawn("sh", ["-c", "echo oops >&2; exit 1"], timeout=10)
    assert result.stderr.strip() == "oops"


async def test_env_set_and_delete(monkeypatch):
    monkeypatch.setenv("AI_CMD_TEST_DEL", "should-disappear")
    result = await spawn(
        "sh",
        ["-c", 'echo "A=${AI_CMD_TEST_DEL:-unset} B=${AI_CMD_TEST_SET:-unset}"'],
        env={"AI_CMD_TEST_DEL": None, "AI_CMD_TEST_SET": "v1"},
        timeout=10,
    )
    assert "A=unset" in result.stdout
    assert "B=v1" in result.stdout


async def test_bad_cwd_raises():
    with pytest.raises(RuntimeError, match="工作目录不存在"):
        await spawn("sh", ["-c", "true"], cwd="/nonexistent/path/xyz", timeout=5)


async def test_cwd_applied(tmp_path):
    result = await spawn("sh", ["-c", "pwd"], cwd=str(tmp_path), timeout=10)
    assert result.stdout.strip() == str(tmp_path)


async def test_timeout_flag():
    result = await spawn("sleep", ["30"], timeout=0.5)
    assert result.timed_out
    assert not result.ok


async def test_timeout_kills_process_group(tmp_path):
    """超时后整个进程组（含孙进程）都必须被杀死。"""
    pidfile = tmp_path / "child.pid"
    result = await spawn(
        "bash",
        ["-c", f"sleep 60 & echo $! > {pidfile}; wait"],
        timeout=1.0,
    )
    assert result.timed_out

    pid = int(pidfile.read_text().strip())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(pid, 9)  # 兜底清理，避免污染测试机
        pytest.fail("孙进程未被杀死（进程组 kill 失效）")


def test_is_available():
    assert is_available("sh")
    assert not is_available("definitely-not-a-real-cli-xyz")


def test_require_cli_raises():
    with pytest.raises(RuntimeError, match="未安装"):
        require_cli("definitely-not-a-real-cli-xyz")
