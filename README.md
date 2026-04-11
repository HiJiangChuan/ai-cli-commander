# ai-commander

一个轻量 MCP server，让 Claude 可以在对话中直接调用 Gemini 和 Codex，把它们变成 Claude Code 里的工具。

## 它做什么

通过 MCP（Model Context Protocol）向 Claude 暴露两个工具：

- **`ask_gemini`** — 将 prompt 发送给 Gemini CLI，返回结果
- **`ask_codex`** — 连接 `codex mcp-server`，调用 Codex

这样你就能在 Claude Code 里做多 AI 协作：把任务分配给 Gemini 或 Codex，对比输出，或者各取所长。

## 前置条件

在开始之前，需要先安装以下工具：

**1. Gemini CLI**

```bash
npm install -g @google/gemini-cli
```

安装后运行 `gemini` 完成 Google 账号授权。

**2. Codex CLI**

```bash
npm install -g @openai/codex
```

安装后配置 OpenAI API Key：

```bash
export OPENAI_API_KEY="your-api-key"
```

**3. uv（Python 包管理器）**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**4. Python 3.14+** 和 **[Claude Code](https://claude.ai/code)**

## 安装

1. 克隆项目：

```bash
git clone https://github.com/HiJiangChuan/ai-commander.git
cd ai-commander
```

2. 安装依赖：

```bash
uv sync
```

3. 在 `~/.claude/mcp.json` 中注册 MCP server：

```json
{
  "mcpServers": {
    "ai-commander": {
      "command": "uv",
      "args": ["run", "python", "main.py"],
      "cwd": "/path/to/ai-commander"
    }
  }
}
```

将 `/path/to/ai-commander` 替换为实际路径。

4. 重启 Claude Code，`ask_gemini` 和 `ask_codex` 工具即可使用。

## 使用

在 Claude Code 会话中，Claude 可以调用：

```
ask_gemini("用一句话解释这个函数。")
ask_codex("把这段代码重构成 async/await 风格。")
```

## License

MIT
