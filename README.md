# ai-commander

一个统一 MCP Server 包，让任意 AI Agent 可以调度 **Gemini、Codex、Kimi、Claude** 四个 AI。

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Gemini    │     │    Codex    │     │    Kimi     │     │   Claude    │
│  gemini-cli │     │  codex-cli  │     │  kimi-cli   │     │ claude-cli  │
│  --acp      │     │ app-server  │     │    acp      │     │  -p --json  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │                   │
       └───────────────────┴───────────────────┴───────────────────┘
                                   │
                          ┌────────┴────────┐
                          │  ai-commander   │
                          │  (统一 MCP 包)   │
                          └────────┬────────┘
                                   │
                          ┌────────┴────────┐
                          │   Agent 主脑     │
                          │ Claude / Cursor │
                          │   / Kimi 等     │
                          └─────────────────┘
```

## 前置条件

安装四个 AI 的 CLI，并分别登录：

```bash
# Gemini
npm install -g @google/gemini-cli
gemini auth login

# Codex
npm install -g @openai/codex
codex login

# Kimi
curl -fsSL https://code.kimi.com/install.sh | bash
kimi /login

# Claude
npm install -g @anthropic-ai/claude-code
claude /login
```

以及 [uv](https://docs.astral.sh/uv/)：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 安装

```bash
git clone https://github.com/HiJiangChuan/ai-commander.git
cd ai-commander
uv tool install .
```

一次安装获得 4 个命令：`gemini-server`、`codex-server`、`kimi-server`、`claude-server`。

更新：

```bash
cd ai-commander && git pull
uv tool install --force .
```

卸载：

```bash
uv tool uninstall ai-commander
```

## 配置 MCP

**Claude Desktop**（macOS）：

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "gemini": { "command": "gemini-server" },
    "codex":  { "command": "codex-server" },
    "kimi":   { "command": "kimi-server" },
    "claude": { "command": "claude-server" }
  }
}
```

保存后重启 Claude Desktop。

## 使用

注册完成后，Agent 即可调用：

```
ask_gemini("用一句话解释这个函数。")
ask_codex("把这段代码重构成 async/await 风格。")
ask_kimi("分析这个中文文档的核心观点。")
ask_claude("审查这个方案的安全性。")
```

## 能力分工

| 工具 | 模型 | 最佳场景 |
|------|------|---------|
| `ask_gemini` | Gemini 2.5 Pro | 长上下文（200万 token）、Google 搜索、创意写作 |
| `ask_codex` | GPT-5.4 | 代码生成、ChatGPT 订阅免费额度 |
| `ask_kimi` | Kimi K2.5 | 中文代码/文档、国内网络优化 |
| `ask_claude` | Sonnet 4.6 | 复杂推理、多文件编辑、工具调用 |

## 项目结构

```
ai-commander/
├── pyproject.toml              # 包配置
├── README.md                   # 本文档
├── mcp-config-example.json     # MCP 配置示例
├── uv.lock                     # 依赖锁定
└── src/ai_commander/
    ├── __init__.py
    ├── core.py                 # 公共逻辑：HTTP 客户端、并发控制、日志
    ├── gemini.py               # Gemini MCP Server
    ├── codex.py                # Codex MCP Server
    ├── kimi.py                 # Kimi MCP Server
    └── claude.py               # Claude MCP Server
```

## License

MIT
