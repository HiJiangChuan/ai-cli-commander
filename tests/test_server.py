"""server 层测试：agent 名单解析、semaphore 限流、ask_all 汇总、health 格式。"""
from __future__ import annotations

import asyncio

import pytest

from ai_commander import server
from ai_commander.server import _parse_agents, _run, ask_all, health


@pytest.fixture(autouse=True)
def _fresh_semaphores():
    """semaphore 绑定事件循环，pytest 每个测试一个新循环，必须清空重建。"""
    server._sem.clear()
    yield
    server._sem.clear()


class _FakeAgent:
    """替身 agent 模块：满足 _AGENTS 的接口约定。"""

    def __init__(self, display: str, reply: str = "", error: str = ""):
        self.DISPLAY_NAME = display
        self._reply = reply
        self._error = error

    async def call(self, prompt: str, *, cwd: str | None = None) -> str:
        if self._error:
            raise RuntimeError(self._error)
        return f"{self._reply}:{prompt}"

    async def health_check(self) -> dict:
        return {"agent": self.DISPLAY_NAME, "available": True, "ok": True, "latency_ms": 5}


# ── _parse_agents ─────────────────────────────────────────────────────────────


def test_parse_agents_basic():
    assert _parse_agents("agy,codex,kimi") == ["agy", "codex", "kimi"]


def test_parse_agents_normalizes_and_dedupes():
    assert _parse_agents(" AGY , codex ,agy,, unknown ") == ["agy", "codex"]


def test_parse_agents_empty():
    assert _parse_agents("") == []
    assert _parse_agents("nope,nothing") == []


# ── _run：限流与错误传播 ──────────────────────────────────────────────────────


async def test_run_returns_result():
    async def work():
        return "done"

    assert await _run("testagent", work()) == "done"


async def test_run_propagates_exception():
    async def boom():
        raise RuntimeError("炸了")

    with pytest.raises(RuntimeError, match="炸了"):
        await _run("testagent", boom())


async def test_run_respects_concurrency_limit(monkeypatch):
    monkeypatch.setenv("TESTAGENT_MAX_CONCURRENT", "2")

    active = 0
    peak = 0

    async def work():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return "ok"

    results = await asyncio.gather(*(_run("testagent", work()) for _ in range(6)))
    assert results == ["ok"] * 6
    assert peak <= 2


async def test_run_bad_env_limit_falls_back(monkeypatch):
    monkeypatch.setenv("TESTAGENT_MAX_CONCURRENT", "not-a-number")

    async def work():
        return "ok"

    assert await _run("testagent", work()) == "ok"


# ── ask_all ───────────────────────────────────────────────────────────────────


async def test_ask_all_goes_through_semaphore(monkeypatch):
    """回归测试：ask_all 的并行分支必须经过 per-agent semaphore。"""
    monkeypatch.setenv("AGY_MAX_CONCURRENT", "1")

    active = 0
    peak = 0

    class _CountingAgent(_FakeAgent):
        async def call(self, prompt: str, *, cwd: str | None = None) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return "ok"

    monkeypatch.setitem(server._AGENTS, "agy", _CountingAgent("FakeAgy"))

    # 两个 ask_all 并发触发同一个 agent → 限流应生效
    await asyncio.gather(
        ask_all("q1", agents="agy"),
        ask_all("q2", agents="agy"),
    )
    assert peak == 1


async def test_ask_all_mixed_success_and_failure(monkeypatch):
    monkeypatch.setitem(server._AGENTS, "agy", _FakeAgent("FakeAgy", reply="回答"))
    monkeypatch.setitem(server._AGENTS, "codex", _FakeAgent("FakeCodex", error="挂了"))

    out = await ask_all("你好", agents="agy,codex")
    assert "## FakeAgy" in out
    assert "回答:你好" in out
    assert "## FakeCodex" in out
    assert "❌ 失败：挂了" in out
    assert "总耗时" in out


async def test_ask_all_no_valid_agents():
    out = await ask_all("hi", agents="nothing,invalid")
    assert "没有有效的 agent 名称" in out


async def test_ask_all_passes_cwd(monkeypatch, tmp_path):
    seen: list[str | None] = []

    class _CwdAgent(_FakeAgent):
        async def call(self, prompt: str, *, cwd: str | None = None) -> str:
            seen.append(cwd)
            return "ok"

    monkeypatch.setitem(server._AGENTS, "agy", _CwdAgent("FakeAgy"))
    await ask_all("hi", agents="agy", cwd=str(tmp_path))
    assert seen == [str(tmp_path)]


# ── health ────────────────────────────────────────────────────────────────────


async def test_health_formats_all_states(monkeypatch):
    ok_agent = _FakeAgent("OkAgent")

    class _MissingAgent(_FakeAgent):
        async def health_check(self) -> dict:
            return {"agent": "x", "available": False, "error": "没装"}

    class _SickAgent(_FakeAgent):
        async def health_check(self) -> dict:
            return {"agent": "y", "available": True, "ok": False, "error": "响应异常"}

    class _NoteAgent(_FakeAgent):
        async def health_check(self) -> dict:
            return {"agent": "z", "available": True, "ok": True, "note": "仅验证存在"}

    monkeypatch.setattr(server, "_AGENTS", {
        "agy": ok_agent,
        "codex": _MissingAgent("MissingAgent"),
        "kimi": _SickAgent("SickAgent"),
        "claude": _NoteAgent("NoteAgent"),
    })

    out = await health()
    assert "✅ OkAgent：正常（5ms）" in out
    assert "❌ MissingAgent" in out and "没装" in out
    assert "⚠️  SickAgent" in out and "响应异常" in out
    assert "✅ NoteAgent：正常（仅验证存在）" in out


async def test_tools_are_registered():
    tools = {t.name for t in await server.mcp.list_tools()}
    assert tools == {"ask_agy", "ask_codex", "ask_kimi", "ask_claude", "ask_all", "health"}
