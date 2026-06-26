"""
ai-commander MCP server —— 统一入口，注册所有 AI 工具。

工具列表：
  ask_agy     → Antigravity CLI (Gemini Flash)
  ask_codex   → Codex CLI (GPT-5.x)
  ask_kimi    → Kimi CLI (K2.5)
  ask_claude  → Claude Code CLI (Sonnet 4.x)
  ask_all     → 并行调用多个 AI，汇总结果
  health      → 检查各 CLI 是否可用

单次调用用 ask_xxx，并行评审用 ask_all。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP

from ai_commander.agents import agy, claude, codex, kimi

# ── 日志配置 ─────────────────────────────────────────────────────────────────

LOG_DIR = os.path.expanduser(os.getenv("LOG_DIR", "~/.ai-commander/logs"))


def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger("ai_commander")
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(os.path.join(LOG_DIR, "ai-commander.log"))
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)


logger = logging.getLogger("ai_commander.server")

# ── FastMCP 实例 ──────────────────────────────────────────────────────────────

mcp = FastMCP("ai-commander")

# ── 工具包装器：统一错误处理 + 计时日志 ───────────────────────────────────────

async def _run(name: str, coro, ctx: Context) -> str:
    t0 = time.monotonic()
    try:
        result = await coro
        logger.info("[%s] OK %.1fs", name, time.monotonic() - t0)
        return result
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.error("[%s] FAIL %.1fs: %s", name, elapsed, exc)
        return f"[{name} 失败] {exc}"


# ── ask_agy ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def ask_agy(prompt: str, ctx: Context) -> str:
    """向 Antigravity CLI (agy / Gemini Flash) 发送提问，返回回复。"""
    return await _run("agy", agy.call(prompt), ctx)


# ── ask_codex ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def ask_codex(
    prompt: str,
    ctx: Context,
    reasoning: str = "medium",
) -> str:
    """向 Codex CLI (GPT-5.x) 发送提问，返回回复。

    Args:
        prompt: 任务或问题。
        reasoning: 推理深度 —— low / medium / high / xhigh（默认 medium）。
    """
    return await _run("codex", codex.call(prompt, reasoning=reasoning), ctx)


# ── ask_kimi ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def ask_kimi(prompt: str, ctx: Context) -> str:
    """向 Kimi CLI (K2.5) 发送提问，返回回复。"""
    return await _run("kimi", kimi.call(prompt), ctx)


# ── ask_claude ────────────────────────────────────────────────────────────────

@mcp.tool()
async def ask_claude(
    prompt: str,
    ctx: Context,
    max_turns: int = 10,
    allowed_tools: str = "Read,Bash,Edit",
) -> str:
    """向 Claude Code CLI (Sonnet 4.x) 发送任务，返回回复。

    Args:
        prompt: 任务或问题。
        max_turns: Agent 最大循环轮数（默认 10）。
        allowed_tools: 允许的工具列表（逗号分隔，默认 Read,Bash,Edit）。
    """
    return await _run(
        "claude",
        claude.call(prompt, max_turns=max_turns, allowed_tools=allowed_tools),
        ctx,
    )


# ── ask_all ───────────────────────────────────────────────────────────────────

_AGENT_MAP = {
    "agy": agy.call,
    "codex": lambda p: codex.call(p),
    "kimi": kimi.call,
    "claude": lambda p: claude.call(p),
}

_AGENT_DISPLAY = {
    "agy": agy.DISPLAY_NAME,
    "codex": codex.DISPLAY_NAME,
    "kimi": kimi.DISPLAY_NAME,
    "claude": claude.DISPLAY_NAME,
}


@mcp.tool()
async def ask_all(
    prompt: str,
    ctx: Context,
    agents: str = "agy,codex,kimi",
) -> str:
    """并行向多个 AI 发送相同提问，汇总所有回复。

    Args:
        prompt: 发给所有 AI 的相同问题或任务。
        agents: 逗号分隔的 AI 列表（可选：agy, codex, kimi, claude），默认 agy,codex,kimi。

    适用场景：
      - 3 个 AI 隔离评审同一段代码 / 文章
      - 对比不同 AI 的答案
      - 汇总后由主 AI 综合分析
    """
    selected = [a.strip() for a in agents.split(",") if a.strip() in _AGENT_MAP]
    if not selected:
        return "[ask_all] 没有有效的 agent 名称。可选：agy, codex, kimi, claude"

    t0 = time.monotonic()
    tasks = {name: asyncio.create_task(_AGENT_MAP[name](prompt)) for name in selected}

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    lines = [f"# ask_all 并行结果（{len(selected)} 个 AI）\n"]
    for name, result in zip(tasks.keys(), results):
        display = _AGENT_DISPLAY.get(name, name)
        lines.append(f"## {display}")
        if isinstance(result, Exception):
            lines.append(f"❌ 失败：{result}\n")
        else:
            lines.append(f"{result}\n")

    elapsed = time.monotonic() - t0
    lines.append(f"---\n总耗时：{elapsed:.1f}s（并行，取最慢 AI 的时间）")

    logger.info("[ask_all] %s done %.1fs", selected, elapsed)
    return "\n".join(lines)


# ── health ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def health(ctx: Context) -> str:
    """检查所有 AI CLI 是否已安装并可用。"""
    checks = await asyncio.gather(
        agy.health_check(),
        codex.health_check(),
        kimi.health_check(),
        claude.health_check(),
        return_exceptions=True,
    )

    lines = ["# AI Commander 健康检查\n"]
    for check in checks:
        if isinstance(check, Exception):
            lines.append(f"- ❓ 检查失败：{check}")
            continue
        name = check.get("agent", "?")
        display = _AGENT_DISPLAY.get(name, name)
        if not check.get("available"):
            lines.append(f"- ❌ {display}：CLI 未安装 —— {check.get('error', '')}")
        elif check.get("ok") is False:
            lines.append(f"- ⚠️  {display}：已安装但响应异常 —— {check.get('error', '')}")
        else:
            ms = check.get("latency_ms", "")
            ms_str = f"（{ms}ms）" if ms else ""
            lines.append(f"- ✅ {display}：正常{ms_str}")

    return "\n".join(lines)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _setup_logging()
    logger.info("ai-commander-server 启动（Python %s）", sys.version.split()[0])
    mcp.run()


if __name__ == "__main__":
    main()
