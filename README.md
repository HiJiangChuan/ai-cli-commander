# ai-cli-commander

**MCP server that lets any AI agent orchestrate multiple LLM CLIs — Antigravity (agy), Codex, Kimi, and Claude — through a single unified interface.**

> One agent to rule them all. Ask GPT, Kimi, Gemini, and Claude in parallel, from any MCP-compatible host (Claude Code, OpenClaw, Cursor, etc.).

---

## What is this?

`ai-cli-commander` 是一个 MCP Server，把四个主流 AI 的 CLI 工具包装成统一的 MCP 工具，让任意 AI Agent 主脑可以跨模型调度：

| MCP 工具 | 背后的 AI | 最佳场景 |
|---------|----------|---------|
| `ask_agy` | Antigravity / Gemini | Google 生态、长上下文 |
| `ask_codex` | OpenAI Codex / GPT | 代码生成、ChatGPT 订阅免费额度 |
| `ask_kimi` | Kimi K2.5（月之暗面）| 中文代码/文档、国内网络 |
| `ask_claude` | Claude Code | 复杂推理、多文件编辑、工具调用 |
| `ask_all` | 以上任意组合并发 | 并行获取多模型意见 |
| `health` | — | 检查各 CLI 是否安装可用 |

**典型用法**：让 Claude Code 同时咨询 GPT 和 Kimi，比对答案；或让 Cursor 调用 Claude 做二次审查。

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Agent Host（Claude Code / Cursor / OpenClaw / 任意 MCP 客户端）│
│  负责推理、规划、决定调用哪个外部 AI                             │
└─────────────────────────────────────────────────────────────┘
                             │  MCP Protocol
                             ▼
┌─────────────────────────────────────────────────────────────┐
│              ai-cli-commander MCP Server                    │
│  ask_agy() │ ask_codex() │ ask_kimi() │ ask_claude()        │
│              ask_all()（并发） │ health()                    │
└─────────────────────────────────────────────────────────────┘
        │           │           │           │
        ▼           ▼           ▼           ▼
  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────────┐
  │   agy   │ │  Codex  │ │  Kimi   │ │   Claude    │
  │--print  │ │  exec   │ │ ACP CLI │ │ -p --json   │
  │ stdout  │ │ + stdin │ │JSON-RPC │ │ stdin→JSON  │
  └─────────┘ └─────────┘ └─────────┘ └─────────────┘
```

### 四个 AI 的底层协议差异

| AI | 调用方式 | 协议 |
|----|---------|------|
| **agy** | `agy --print <prompt>` | CLI stdout |
| **Codex** | `codex exec -`（prompt 走 stdin，结果走临时文件） | CLI |
| **Kimi** | `kimi acp` | ACP (JSON-RPC 2.0 over NDJSON/stdio)，含 fs/terminal 反向请求 |
| **Claude** | `claude -p --output-format json`（prompt 走 stdin） | CLI stdout JSON |

可靠性设计（都在 `spawn.py` / `acp.py`）：

- 子进程 stdin 默认接 DEVNULL，绝不继承 MCP 协议管道（否则 CLI 读 stdin 会吃掉 MCP 消息）
- `start_new_session=True` 独立进程组；超时后 `killpg` 清理整个进程树，不留孙进程
- 注入 TERM 等终端环境变量，避免 CLI 检测不到 TTY 而挂起
- per-agent semaphore 并发限流，`ask_all` 的并行分支同样受限
- Kimi 握手阶段有独立超时，CLI 卡死不会导致工具调用永久挂起

> **注意**：旧版 Gemini CLI（`gemini --acp`）自 2026年6月18日起对个人 Pro 用户停止服务，已由 Antigravity CLI (`agy`) 替代。

---

## ⚠️ Security

这是一个**个人效率工具**，为了让子 AI 能自主干活，权限默认从宽，使用前请知晓：

- `ask_agy` 带 `--dangerously-skip-permissions`；`ask_kimi` 会自动批准 Kimi 的所有权限请求（含读写文件、执行 shell 命令）。**发给这些工具的 prompt 如果包含提示注入，等于在你机器上执行任意命令。**
- `ask_claude` 通过 `allowed_tools` 白名单限权（默认 `Read,Bash,Edit`，白名单外的工具会被自动拒绝）；`ask_codex` 使用 `--sandbox read-only`。
- agy 的 prompt 经命令行参数传递，多用户系统上对 `ps` 可见（codex / claude 走 stdin，无此问题）。
- 日志（默认 `~/.ai-cli-commander/logs`）不记录 prompt 内容，只记录命令名、耗时和错误。

不要把这个 server 暴露给不受信任的调用方。

---

## Prerequisites

**1. Antigravity CLI (agy)**
```bash
# 首次运行时按提示通过浏览器完成 OAuth 授权（同时会启动守护进程）
agy
```

**2. Codex CLI**
```bash
npm install -g @openai/codex
codex login
```

**3. Kimi CLI**
```bash
# macOS / Linux
curl -fsSL https://code.kimi.com/install.sh | bash
# 或
uv tool install --python 3.13 kimi-cli

kimi --version
kimi /login
```

**4. Claude Code**
```bash
npm install -g @anthropic-ai/claude-code
```

**5. uv（Python 包管理器）**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Installation

```bash
git clone https://github.com/HiJiangChuan/ai-cli-commander.git
cd ai-cli-commander

# 安装统一包（一次安装，获得所有命令）
uv tool install .
```

安装后命令出现在 `~/.local/bin/`，可直接执行，不依赖项目目录。

**更新：**
```bash
cd ai-cli-commander && git pull
uv tool install --force .
```

**卸载：**
```bash
uv tool uninstall ai-cli-commander
```

---

## Register MCP Server

在 `~/.claude.json` 中注册（Claude Code 用户）：

```json
{
  "mcpServers": {
    "ai-cli-commander": {
      "command": "ai-cli-commander-server"
    }
  }
}
```

完整配置示例见 [mcp-config-example.json](mcp-config-example.json)。只需注册这**一个** server，它包含全部 6 个工具（`agy-server` 等旧命令名仍保留，但都是同一 server 的别名，不要重复注册）。

OpenClaw / Cursor 等 MCP 兼容客户端同理挂载。

---

## Usage

注册完成后，Agent 主脑可以调用：

```
ask_agy("用一句话解释这个函数。")
ask_codex("把这段代码重构成 async/await 风格。", reasoning="high")
ask_kimi("分析这个中文文档的核心观点。", cwd="/path/to/project")
ask_claude("审查这个方案的安全性。", allowed_tools="Read")

# 并发询问多个 AI，对比答案
ask_all("这个算法的时间复杂度是多少？", agents="agy,codex,kimi,claude")

# 检查各 CLI 状态
health()
```

所有工具都支持可选的 `cwd` 参数，指定子 AI 的工作目录（默认继承 server 进程目录）。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AGY_TIMEOUT` / `CODEX_TIMEOUT` / `KIMI_TIMEOUT` / `CLAUDE_TIMEOUT` | 180 / 300 / 300 / 300 | 单次调用超时（秒） |
| `KIMI_HANDSHAKE_TIMEOUT` | 30 | Kimi ACP 握手超时（秒） |
| `AGY_MODEL` / `CODEX_MODEL` / `CLAUDE_MODEL` | （CLI 默认） | 覆盖模型 |
| `CODEX_REASONING` | medium | minimal / low / medium / high / xhigh |
| `CLAUDE_MAX_TURNS` | 10 | Claude agent 最大轮数 |
| `AGY_MAX_CONCURRENT` 等 `*_MAX_CONCURRENT` | agy 2 / codex 3 / kimi 2 / claude 2 | per-agent 并发上限 |
| `AI_COMMANDER_LOG_DIR` | `~/.ai-cli-commander/logs` | 日志目录（自动轮转，5MB × 3） |
| `AI_COMMANDER_LOG_LEVEL` | INFO | 日志级别 |

---

## Project Structure

```
ai-cli-commander/
├── pyproject.toml                 # 包配置 + pytest / ruff 配置
├── README.md
├── mcp-config-example.json        # MCP 注册配置示例
├── tests/                         # pytest 测试（无需安装任何 AI CLI 即可运行）
│
└── src/ai_commander/
    ├── __init__.py
    ├── server.py                  # MCP Server 入口：工具注册、并发限流、日志
    ├── spawn.py                   # 子进程层：隔离、超时、进程组清理
    ├── acp.py                     # ACPClient：JSON-RPC 2.0 over NDJSON/stdio
    └── agents/
        ├── agy.py                 # Antigravity handler
        ├── codex.py               # Codex handler
        ├── kimi.py                # Kimi handler（ACP + fs/terminal 反向请求）
        └── claude.py              # Claude handler
```

---

## Development

```bash
uv sync            # 安装依赖（含 dev 组）
uv run pytest      # 跑测试（不需要安装任何 AI CLI）
uv run ruff check .
```

---

## License

MIT
