# ai-commander

A thin MCP server that lets Claude call Gemini and Codex directly — turning them into callable tools within a Claude Code session.

## What it does

Exposes two tools to Claude via the Model Context Protocol (MCP):

- **`ask_gemini`** — sends a prompt to Gemini CLI and returns the response
- **`ask_codex`** — connects to `codex mcp-server` and calls Codex

This lets you run multi-AI workflows from inside Claude Code: delegate tasks to Gemini or Codex, compare outputs, or use each model where it's strongest.

## Requirements

- [Claude Code](https://claude.ai/code)
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) — `gemini` must be in PATH
- [Codex CLI](https://github.com/openai/codex) — `codex` must be in PATH
- [uv](https://github.com/astral-sh/uv)
- Python 3.14+

## Setup

1. Clone the repo:

```bash
git clone https://github.com/HiJiangChuan/ai-commander.git
cd ai-commander
```

2. Install dependencies:

```bash
uv sync
```

3. Register as an MCP server in `~/.claude/mcp.json`:

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

4. Restart Claude Code. The `ask_gemini` and `ask_codex` tools will be available.

## Usage

Inside a Claude Code session, Claude can call:

```
ask_gemini("Explain this function in one sentence.")
ask_codex("Refactor this code to use async/await.")
```

## License

MIT
