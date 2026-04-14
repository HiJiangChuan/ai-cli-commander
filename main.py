"""
ai-commander: thin MCP server bridging Claude to Gemini and Codex.

Exposes two tools to Claude:
  - ask_gemini(prompt)  → calls Gemini CLI via subprocess
  - ask_codex(prompt)   → connects to `codex mcp-server` via MCP stdio
"""

import asyncio
import logging

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# Suppress noisy codex/event validation warnings from MCP SDK
logging.getLogger("root").setLevel(logging.ERROR)

app = Server("ai-commander")

GEMINI_TIMEOUT = 180  # seconds
CODEX_TIMEOUT = 300   # codex is slower; give it 5 minutes
CODEX_MODEL = "gpt-5.4"


# ── Gemini ────────────────────────────────────────────────────────────────────

async def _call_gemini(prompt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gemini", "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GEMINI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"Gemini CLI timed out after {GEMINI_TIMEOUT}s")

    if proc.returncode != 0:
        err = stderr.decode().strip()
        raise RuntimeError(f"Gemini CLI error (exit {proc.returncode}): {err}")
    return stdout.decode().strip()


# ── Codex ─────────────────────────────────────────────────────────────────────

async def _call_codex(prompt: str) -> str:
    params = StdioServerParameters(command="codex", args=["mcp-server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                result = await asyncio.wait_for(
                    session.call_tool("codex", {"prompt": prompt, "model": CODEX_MODEL}),
                    timeout=CODEX_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Codex timed out after {CODEX_TIMEOUT}s")

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
            description="Send a prompt to Gemini (gemini-2.5-pro) and return its response.",
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
            description="Send a prompt to Codex (gpt-5.4) and return its response.",
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

    try:
        if name == "ask_gemini":
            text = await _call_gemini(prompt)
        elif name == "ask_codex":
            text = await _call_codex(prompt)
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        # Return error as text content instead of crashing the server
        return [types.TextContent(type="text", text=f"[ERROR] {type(e).__name__}: {e}")]

    return [types.TextContent(type="text", text=text)]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
