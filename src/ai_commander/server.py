"""
ai-cli-commander MCP server —— 统一入口，注册所有 AI 工具。

工具列表：
  ask_agy     → Antigravity CLI (Gemini)
  ask_codex   → Codex CLI (GPT)
  ask_kimi    → Kimi CLI (K2.5)
  ask_claude  → Claude Code CLI
  ask_all     → 并行调用多个 AI，汇总结果
  health      → 检查各 CLI 是否可用

并发模型：
  每个 MCP 客户端 session 启动一个独立 server 进程，多个 session 之间互不干扰。
  同一进程内的所有调用（包括 ask_all 的并行分支）都经过 per-agent semaphore：
    AGY_MAX_CONCURRENT    默认 2（agy daemon 连接数有限）
    CODEX_MAX_CONCURRENT  默认 3
    KIMI_MAX_CONCURRENT   默认 2
    CLAUDE_MAX_CONCURRENT 默认 2

错误模型：
  单个工具失败时抛出异常，由 FastMCP 标记为 isError 返回给客户端；
  ask_all 中单个 AI 失败不影响其他 AI，失败信息汇总在结果文本里。

日志：
  写入 AI_COMMANDER_LOG_DIR（默认 ~/.ai-cli-commander/logs），自动轮转。
  级别由 AI_COMMANDER_LOG_LEVEL 控制（默认 INFO）。
  绝不写 stdout —— 那是 MCP 协议管道。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from mcp.server.fastmcp import FastMCP

from ai_commander.agents import agy, claude, codex, kimi

# ── 日志配置 ─────────────────────────────────────────────────────────────────

LOG_DIR = os.path.expanduser(
    os.getenv("AI_COMMANDER_LOG_DIR") or os.getenv("LOG_DIR") or "~/.ai-cli-commander/logs"
)
LOG_LEVEL = os.getenv("AI_COMMANDER_LOG_LEVEL", "INFO").upper()


def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger("ai_commander")
    if root.handlers:
        return
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "ai-cli-commander.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)


logger = logging.getLogger("ai_commander.server")

# ── FastMCP 实例 ──────────────────────────────────────────────────────────────

mcp = FastMCP("ai-cli-commander")

# ── Agent 注册表 ──────────────────────────────────────────────────────────────
# 每个模块须提供：call(prompt, *, cwd=None, ...)、health_check()、DISPLAY_NAME

_AGENTS = {
    "agy": agy,
    "codex": codex,
    "kimi": kimi,
    "claude": claude,
}

# ── 并发限流 ──────────────────────────────────────────────────────────────────
# 每个 agent 独立的 semaphore，在 server 进程内全局共享。
# 所有工具（含 ask_all 的并行分支）都必须经过 _run()，确保限流不被绕过。

_DEFAULT_LIMITS = {"agy": 2, "codex": 3, "kimi": 2, "claude": 2}

_sem: dict[str, asyncio.Semaphore] = {}


def _get_sem(name: str) -> asyncio.Semaphore:
    if name not in _sem:
        default = _DEFAULT_LIMITS.get(name, 3)
        try:
            limit = int(os.getenv(f"{name.upper()}_MAX_CONCURRENT", "") or default)
        except ValueError:
            limit = default
        _sem[name] = asyncio.Semaphore(max(1, limit))
    return _sem[name]


async def _run(name: str, coro) -> str:
    """semaphore 限流 + 计时日志。失败时抛出异常，由调用方决定如何呈现。"""
    sem = _get_sem(name)
    t0 = time.monotonic()
    async with sem:
        try:
            result = await coro
            logger.info("[%s] OK %.1fs", name, time.monotonic() - t0)
            return result
        except Exception as exc:
            logger.error("[%s] FAIL %.1fs: %s", name, time.monotonic() - t0, exc)
            raise


# ── 单 agent 工具 ─────────────────────────────────────────────────────────────


@mcp.tool()
async def ask_agy(prompt: str, cwd: str = "") -> str:
    """向 Antigravity CLI (agy / Gemini) 发送提问，返回回复。

    Args:
        prompt: 任务或问题。
        cwd: 子进程工作目录（可选，默认继承 server 进程目录）。
    """
    return await _run("agy", agy.call(prompt, cwd=cwd or None))


@mcp.tool()
async def ask_codex(
    prompt: str,
    reasoning: str = "medium",
    model: str = "",
    cwd: str = "",
) -> str:
    """向 Codex CLI (GPT) 发送提问，返回回复。

    Args:
        prompt: 任务或问题。
        reasoning: 推理深度 —— minimal / low / medium / high / xhigh（默认 medium）。
        model: 覆盖默认模型（可选）。
        cwd: 子进程工作目录（可选，默认继承 server 进程目录）。
    """
    return await _run("codex", codex.call(prompt, reasoning=reasoning, model=model, cwd=cwd or None))


@mcp.tool()
async def ask_kimi(prompt: str, cwd: str = "") -> str:
    """向 Kimi CLI (K2.5) 发送提问，返回回复。

    Args:
        prompt: 任务或问题。
        cwd: Kimi 会话工作目录（可选，默认继承 server 进程目录）。
    """
    return await _run("kimi", kimi.call(prompt, cwd=cwd or None))


@mcp.tool()
async def ask_claude(
    prompt: str,
    max_turns: int = 10,
    allowed_tools: str = "Read,Bash,Edit",
    cwd: str = "",
) -> str:
    """向 Claude Code CLI 发送任务，返回回复。

    Args:
        prompt: 任务或问题。
        max_turns: Agent 最大循环轮数（默认 10）。
        allowed_tools: 允许的工具白名单（逗号分隔，默认 Read,Bash,Edit）。
            白名单外的工具在非交互模式下会被自动拒绝。
        cwd: 子进程工作目录（可选，默认继承 server 进程目录）。
    """
    return await _run(
        "claude",
        claude.call(prompt, max_turns=max_turns, allowed_tools=allowed_tools, cwd=cwd or None),
    )


# ── ask_all ───────────────────────────────────────────────────────────────────


def _parse_agents(agents: str) -> list[str]:
    """解析逗号分隔的 agent 名单：去空白、小写、去重、过滤未知名称。"""
    selected: list[str] = []
    for raw in agents.split(","):
        name = raw.strip().lower()
        if name in _AGENTS and name not in selected:
            selected.append(name)
    return selected


@mcp.tool()
async def ask_all(
    prompt: str,
    agents: str = "agy,codex,kimi",
    cwd: str = "",
) -> str:
    """并行向多个 AI 发送相同提问，汇总所有回复。

    Args:
        prompt: 发给所有 AI 的相同问题或任务。
        agents: 逗号分隔的 AI 列表（可选：agy, codex, kimi, claude），默认 agy,codex,kimi。
        cwd: 子进程工作目录（可选，默认继承 server 进程目录）。

    适用场景：
      - 多个 AI 隔离评审同一段代码 / 文章
      - 对比不同 AI 的答案
      - 汇总后由主 AI 综合分析
    """
    selected = _parse_agents(agents)
    if not selected:
        return "[ask_all] 没有有效的 agent 名称。可选：agy, codex, kimi, claude"

    t0 = time.monotonic()
    results = await asyncio.gather(
        *(_run(name, _AGENTS[name].call(prompt, cwd=cwd or None)) for name in selected),
        return_exceptions=True,
    )

    lines = [f"# ask_all 并行结果（{len(selected)} 个 AI）\n"]
    for name, result in zip(selected, results, strict=True):
        lines.append(f"## {_AGENTS[name].DISPLAY_NAME}")
        if isinstance(result, BaseException):
            lines.append(f"❌ 失败：{result}\n")
        else:
            lines.append(f"{result}\n")

    elapsed = time.monotonic() - t0
    lines.append(f"---\n总耗时：{elapsed:.1f}s（并行，取最慢 AI 的时间）")

    logger.info("[ask_all] %s done %.1fs", selected, elapsed)
    return "\n".join(lines)


# ── health ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def health() -> str:
    """检查所有 AI CLI 是否已安装并可用。"""
    modules = list(_AGENTS.values())
    checks = await asyncio.gather(
        *(m.health_check() for m in modules),
        return_exceptions=True,
    )

    lines = ["# ai-cli-commander 健康检查\n"]
    for module, check in zip(modules, checks, strict=True):
        display = module.DISPLAY_NAME
        if isinstance(check, BaseException):
            lines.append(f"- ❓ {display}：检查失败 —— {check}")
            continue
        if not check.get("available"):
            lines.append(f"- ❌ {display}：CLI 未安装 —— {check.get('error', '')}")
        elif check.get("ok") is False:
            lines.append(f"- ⚠️  {display}：已安装但响应异常 —— {check.get('error', '')}")
        else:
            ms = check.get("latency_ms")
            note = check.get("note")
            suffix = f"（{ms}ms）" if ms else (f"（{note}）" if note else "")
            lines.append(f"- ✅ {display}：正常{suffix}")

    return "\n".join(lines)


# ── 入口 ──────────────────────────────────────────────────────────────────────


def main() -> None:
    _setup_logging()
    logger.info("ai-cli-commander-server 启动（Python %s）", sys.version.split()[0])
    mcp.run()


if __name__ == "__main__":
    main()
