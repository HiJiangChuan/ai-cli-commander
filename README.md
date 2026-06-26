# ai-cli-commander

**MCP server that lets any AI agent orchestrate multiple LLM CLIs — Antigravity (agy), Codex, Kimi, and Claude — through a single unified interface.**

> One agent to rule them all. Ask GPT, Kimi, Gemini, and Claude in parallel, from any MCP-compatible host (Claude Code, OpenClaw, Cursor, etc.).

---

## What is this?

`ai-cli-commander` 是一个 MCP Server 集合，把四个主流 AI 的 CLI 工具包装成统一的 MCP 工具，让任意 AI Agent 主脑可以跨模型调度：

| MCP 工具 | 背后的 AI | 最佳场景 |
|---------|----------|---------|
| `ask_agy` | Antigravity / Gemini Flash | Google 生态、长上下文 |
| `ask_codex` | OpenAI Codex / GPT-5.4 | 代码生成、ChatGPT 订阅免费额度 |
| `ask_kimi` | Kimi K2.5（月之暗面）| 中文代码/文档、国内网络 |
| `ask_claude` | Claude Sonnet 4.6 | 复杂推理、多文件编辑、工具调用 |
| `ask_all` | 以上全部并发 | 并行获取多模型意见 |

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
│                    ask_all()（并发）                         │
└─────────────────────────────────────────────────────────────┘
        │           │           │           │
        ▼           ▼           ▼           ▼
  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────────┐
  │   agy   │ │  Codex  │ │  Kimi   │ │   Claude    │
  │--print  │ │app-svr  │ │ ACP CLI │ │ -p --json   │
  │ stdout  │ │JSON-RPC │ │JSON-RPC │ │ CLI stdout  │
  └─────────┘ └─────────┘ └─────────┘ └─────────────┘
```

### 四个 AI 的底层协议差异

| AI | 启动命令 | 协议 |
|----|---------|------|
| **agy** | `agy --print` | CLI stdout |
| **Kimi** | `kimi acp` | ACP (JSON-RPC over stdio) |
| **Codex** | `codex app-server` | app-server (JSON-RPC over stdio) |
| **Claude** | `claude -p --output-format json --bare` | CLI stdout JSON |

> **注意**：旧版 Gemini CLI（`gemini --acp`）自 2026年6月18日起对个人 Pro 用户停止服务，已由 Antigravity CLI (`agy`) 替代。

---

## Prerequisites

**1. Antigravity CLI (agy)**
```bash
# 首次运行时按提示通过浏览器完成 OAuth 授权
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

完整配置示例见 [mcp-config-example.json](mcp-config-example.json)。

OpenClaw / Cursor 等 MCP 兼容客户端同理挂载。

---

## Usage

注册完成后，Agent 主脑可以调用：

```
ask_agy("用一句话解释这个函数。")
ask_codex("把这段代码重构成 async/await 风格。")
ask_kimi("分析这个中文文档的核心观点。")
ask_claude("审查这个方案的安全性。")

# 并发询问所有 AI，对比答案
ask_all("这个算法的时间复杂度是多少？")
```

---

## Project Structure

```
ai-cli-commander/
├── pyproject.toml                 # 统一包配置
├── README.md
├── mcp-config-example.json        # MCP 注册配置示例
│
└── src/ai_commander/
    ├── __init__.py
    ├── server.py                  # 统一 MCP Server 入口
    ├── core.py                    # 公共代码：ACPClient、LineBuffer、日志、CLI 检查
    ├── agy.py                     # Antigravity handler
    ├── codex.py                   # Codex handler
    ├── kimi.py                    # Kimi handler
    └── claude.py                  # Claude handler
```

---

## License

MIT
