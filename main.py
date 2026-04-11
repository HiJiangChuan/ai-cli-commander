"""
ai-commander: thin MCP server bridging Claude to Gemini and Codex.

Exposes two tools to Claude:
  - ask_gemini(prompt)  → calls Gemini CLI via subprocess
  - ask_codex(prompt)   → connects to `codex mcp-server` via MCP stdio
"""

import asyncio

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

app = Server("ai-commander")

CODEX_CMD = ["node", "/opt/homebrew/bin/codex", "mcp-server"]
GEMINI_CMD = ["gemini"]


# ── Gemini ────────────────────────────────────────────────────────────────────

async def _call_gemini(prompt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *GEMINI_CMD, "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        err = stderr.decode().strip()
        raise RuntimeError(f"Gemini CLI error (exit {proc.returncode}): {err}")
    return stdout.decode().strip()


# ── Codex ─────────────────────────────────────────────────────────────────────

async def _call_codex(prompt: str) -> str:
    params = StdioServerParameters(command=CODEX_CMD[0], args=CODEX_CMD[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("codex", {"prompt": prompt})
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts).strip()


# ── MCP tool definitions ───────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="ask_gemini",
            description="Send a prompt to Gemini and return its response.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The prompt to send to Gemini."}
                },
                "required": ["prompt"],
            },
        ),
        types.Tool(
            name="ask_codex",
            description="Send a prompt to Codex (OpenAI) and return its response.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The prompt to send to Codex."}
                },
                "required": ["prompt"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    prompt = arguments.get("prompt", "")
    if not prompt:
        raise ValueError("prompt is required")

    if name == "ask_gemini":
        text = await _call_gemini(prompt)
    elif name == "ask_codex":
        text = await _call_codex(prompt)
    else:
        raise ValueError(f"Unknown tool: {name}")

    return [types.TextContent(type="text", text=text)]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
