# ai-commander

四个独立 MCP Server，让任意 AI Agent（OpenClaw、Hermes、Claude Code 等）可以统一调度 **Gemini、Codex、Kimi、Claude** 四个 AI。

---

## 架构设计

### 为什么拆成四个独立 MCP Server？

| 原则 | 说明 |
|------|------|
| **独立生命周期** | 任一 Server 挂了不影响其他 AI |
| **独立协议** | 四个 AI 的底层通信协议完全不同，必须独立实现 |
| **按需挂载** | Agent 可以只装需要的，不必全装 |
| **独立部署** | 未来可以单独拆出去发 PyPI 包 |

### 整体架构

```
┌────────────────────────────────────────────────────────────┐
│  Agent 主脑（任意模型：Kimi / Minimax / Claude / GPT / ...） │
│  ── 负责推理、规划、决定调用哪个外部 AI                       │
└────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────┐
│                    统一调度层：MCP 协议                      │
│   ask_gemini() │ ask_codex() │ ask_kimi() │ ask_claude()   │
└────────────────────────────────────────────────────────────┘
            │           │           │           │
            ▼           ▼           ▼           ▼
      ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────────┐
      │ Gemini  │ │  Codex  │ │  Kimi   │ │   Claude    │
      │--acp CLI│ │app-svr  │ │ acp CLI │ │ -p --json   │
      │ACP协议  │ │app-svr  │ │ ACP协议 │ │ CLI stdout  │
      └─────────┘ └─────────┘ └─────────┘ └─────────────┘
```

### 四个 AI 的底层协议差异

| AI | 启动命令 | 协议 | 核心流程 |
|----|---------|------|---------|
| **Gemini** | `gemini --acp` | ACP (JSON-RPC over stdio) | `initialize` → `session/new` → `session/prompt` → `session/update` 通知收集文本 |
| **Kimi** | `kimi acp` | ACP (JSON-RPC over stdio) | `initialize` → `session/new` → `session/prompt` → `session/update` 通知收集文本 |
| **Codex** | `codex app-server` | app-server (JSON-RPC over stdio) | `initialize` → `initialized` → `thread/start` → `turn/start` → `item/agentMessage/delta` → `turn/completed` |
| **Claude** | `claude -p --output-format json --bare` | CLI stdout JSON | 直接解析子进程 stdout 的 `{result, cost_usd, duration_ms}` |

> **注意**：Gemini 和 Kimi 虽然都叫 ACP，且方法名相同（均使用 `session/prompt`），但底层 CLI 命令、环境变量和 handler 注册仍有差异，因此仍需分别实现。

---

## 前置条件

**1. Gemini CLI**
```bash
npm install -g @google/gemini-cli
gemini auth login
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

## 安装

```bash
git clone https://github.com/HiJiangChuan/ai-commander.git
cd ai-commander

# 安装统一包（一次安装，获得四个命令）
uv tool install .
```

安装后，四个命令会出现在 `~/.local/bin/` 下，可直接执行，不依赖项目目录。

更新时重新执行安装命令即可（加 `--force` 覆盖）：

```bash
cd ai-commander && git pull
uv tool install --force .
```

更新提示词（复制粘贴给任意 AI 即可）：

> 帮我更新 ai-commander。步骤：进入 ~/Developer/ai-commander 目录，git pull 拉取最新代码，然后执行 `uv tool install --force .` 重新安装。完成后告诉我结果。

卸载：

```bash
uv tool uninstall ai-commander
```

---

## 注册 MCP Server

在 `/Users/username/.claude.json` 中注册：

```json
{
  "mcpServers": {
    "gemini": { "command": "gemini-server" },
    "codex": { "command": "codex-server" },
    "kimi": { "command": "kimi-server" },
    "claude": { "command": "claude-server" }
  }
}
```

OpenClaw 用户可在 Skills 中按同样方式挂载四个 MCP Server。

---

## 使用

注册完成后，主 Agent 可以调用：

```
ask_gemini("用一句话解释这个函数。")
ask_codex("把这段代码重构成 async/await 风格。")
ask_kimi("分析这个中文文档的核心观点。")
ask_claude("审查这个方案的安全性。")
```

---

## 四 AI 能力分工建议

| 工具 | 来源 | 最佳场景 |
|------|------|---------|
| `ask_gemini` | `gemini-server` | 长上下文（200万token）、Google搜索、创意写作 |
| `ask_kimi` | `kimi-server` | 中文代码/文档、Kimi K2.5、国内网络优化 |
| `ask_codex` | `codex-server` | GPT-5.4 代码生成、ChatGPT订阅免费额度 |
| `ask_claude` | `claude-server` | Sonnet 4.6 复杂推理、多文件编辑、工具调用最强 |

---

## 项目结构

```
ai-commander/
├── pyproject.toml                 # 统一包配置
├── README.md                      # 本文档
├── mcp-config-example.json        # MCP 注册配置示例
│
└── src/ai_commander/              # 统一源码包
    ├── __init__.py                # 包版本
    ├── core.py                    # 公共代码：ACPClient、LineBuffer、日志、CLI 检查
    ├── gemini.py                  # Gemini MCP Server
    ├── codex.py                   # Codex MCP Server
    ├── kimi.py                    # Kimi MCP Server
    └── claude.py                  # Claude MCP Server
```

---

## License

MIT
