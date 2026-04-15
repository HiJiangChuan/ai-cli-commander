"""
ai-commander: thin MCP server bridging Claude to Gemini and Codex.

Exposes two tools to Claude:
  - ask_gemini(prompt)  → calls `gemini -m gemini-2.5-pro -p <prompt>` via subprocess
  - ask_codex(prompt)   → connects to `codex mcp-server` via MCP stdio

Design principles (informed by gemini-cli-mcp-server, PAL, multicli):
  - Prompts passed via stdin, not command-line args (safe for long inputs)
  - Explicit model flags so behaviour is reproducible
  - Per-call retry with exponential back-off for transient failures
  - All exceptions caught and returned as [ERROR] text; server never crashes
  - Timeouts configurable via environment variables
  - Semaphore-based concurrency control to prevent resource exhaustion
  - Single persistent Codex MCP session (codex mcp-server supports concurrent tool calls)
"""

import asyncio
import logging
import os

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# Suppress the pydantic spam from unknown codex/event notifications
for _noisy in ("mcp.client.session", "mcp.shared.session"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

app = Server("ai-commander")

# ── Configuration (override via env vars) ─────────────────────────────────────
GEMINI_MODEL   = os.getenv("GEMINI_MODEL",   "gemini-2.5-pro")
CODEX_MODEL    = os.getenv("CODEX_MODEL",    "gpt-5.4")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "180"))
CODEX_TIMEOUT  = int(os.getenv("CODEX_TIMEOUT",  "300"))
MAX_RETRIES    = int(os.getenv("AI_MAX_RETRIES", "2"))
GEMINI_MAX_CONCURRENT = int(os.getenv("GEMINI_MAX_CONCURRENT", "4"))
CODEX_MAX_CONCURRENT  = int(os.getenv("CODEX_MAX_CONCURRENT",  "4"))

# Concurrency limiters
_gemini_sem = asyncio.Semaphore(GEMINI_MAX_CONCURRENT)
_codex_sem  = asyncio.Semaphore(CODEX_MAX_CONCURRENT)

# stderr lines that are non-fatal and should be silently dropped
_GEMINI_STDERR_NOISE = (
    "Keychain initialization",
    "keytar",
    "FileKeychain",
    "Loaded cached credentials",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_fatal_stderr(line: str) -> bool:
    return not any(kw in line for kw in _GEMINI_STDERR_NOISE)


async def _with_retry(coro_fn, retries: int = MAX_RETRIES):
    """Run coro_fn(), retrying up to `retries` times with exponential back-off."""
    delay = 3.0
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
    raise last_exc


# ── Gemini ────────────────────────────────────────────────────────────────────

async def _call_gemini_once(prompt: str) -> str:
    """
    Invoke Gemini CLI in headless mode via -p flag.
    Note: gemini ignores stdin when -p is provided and the stdin pipe hangs,
    so we pass the full prompt as the -p argument value directly.
    macOS ARG_MAX (~256 KB) is well above typical review-prompt sizes.
    """
    proc = await asyncio.create_subprocess_exec(
        "gemini", "-m", GEMINI_MODEL, "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=GEMINI_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"Gemini timed out after {GEMINI_TIMEOUT}s")

    if proc.returncode != 0:
        fatal = [l for l in stderr.decode().splitlines() if _is_fatal_stderr(l)]
        raise RuntimeError(
            f"Gemini exit {proc.returncode}: {chr(10).join(fatal) or stderr.decode().strip()}"
        )
    return stdout.decode().strip()


async def _call_gemini(prompt: str) -> str:
    async with _gemini_sem:
        return await _with_retry(lambda: _call_gemini_once(prompt))


# ── Codex persistent session ────────────────────────────────────────────────

class _CodexSession:
    """
    Single persistent Codex MCP session.

    codex mcp-server (Rust/Tokio) supports concurrent tool calls within
    one stdio connection, so a single session is sufficient. The lock only
    guards lazy initialization; actual tool calls run concurrently.
    """

    def __init__(self):
        self._session: ClientSession | None = None
        self._client_ctx = None
        self._session_ctx = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        params = StdioServerParameters(command="codex", args=["mcp-server"])
        self._client_ctx = stdio_client(params)
        read, write = await self._client_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()

    async def call(self, prompt: str) -> str:
        async with self._lock:
            if self._session is None:
                await self._connect()
        result = await asyncio.wait_for(
            self._session.call_tool(
                "codex",
                {"prompt": prompt, "model": CODEX_MODEL},
            ),
            timeout=CODEX_TIMEOUT,
        )
        parts = [b.text for b in result.content if hasattr(b, "text")]
        return "\n".join(parts).strip()

    async def reset(self):
        """Tear down a broken session; next call() will auto-reconnect."""
        async with self._lock:
            for ctx in (self._session_ctx, self._client_ctx):
                if ctx:
                    try:
                        await ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
            self._session = None
            self._client_ctx = None
            self._session_ctx = None


_codex = _CodexSession()


# ── Codex ─────────────────────────────────────────────────────────────────────

async def _call_codex_once(prompt: str) -> str:
    try:
        return await _codex.call(prompt)
    except Exception:
        await _codex.reset()
        raise


async def _call_codex(prompt: str) -> str:
    async with _codex_sem:
        return await _with_retry(lambda: _call_codex_once(prompt))


# ── MCP tool definitions ───────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="ask_gemini",
            description=f"Send a prompt to Gemini ({GEMINI_MODEL}) and return its response.",
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
            description=f"Send a prompt to Codex ({CODEX_MODEL}) and return its response.",
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
    try:
        prompt = arguments.get("prompt", "")
        if not prompt:
            raise ValueError("prompt is required")

        if name == "ask_gemini":
            text = await _call_gemini(prompt)
        elif name == "ask_codex":
            text = await _call_codex(prompt)
        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as exc:
        # Return error as text — never crash the server process
        return [types.TextContent(type="text", text=f"[ERROR] {type(exc).__name__}: {exc}")]

    return [types.TextContent(type="text", text=text)]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
