"""
ai-commander: thin MCP server bridging Claude to Gemini and Codex.

Exposes two tools:
  - ask_gemini(prompt) → Gemini via ACP protocol (JSON-RPC over stdio)
  - ask_codex(prompt)  → Codex via app-server protocol (JSON-RPC over stdio)

Both use the same ACPClient (JSON-RPC 2.0 over NDJSON/stdio).

Design principles:
  - Per-call subprocess isolation (no shared sessions, safe under concurrency)
  - Semaphore-based concurrency control
  - All exceptions caught and returned as [ERROR] text; server never crashes
  - LineBuffer handles large NDJSON lines (bypasses asyncio 64KB limit)
"""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

from mcp.server.fastmcp import FastMCP

# ── Configuration ────────────────────────────────────────────────────────────

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.4")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "180"))
CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "300"))
GEMINI_MAX_CONCURRENT = int(os.getenv("GEMINI_MAX_CONCURRENT", "4"))
CODEX_MAX_CONCURRENT = int(os.getenv("CODEX_MAX_CONCURRENT", "4"))

_gemini_sem = asyncio.Semaphore(GEMINI_MAX_CONCURRENT)
_codex_sem = asyncio.Semaphore(CODEX_MAX_CONCURRENT)


# ── LineBuffer ───────────────────────────────────────────────────────────────

class LineBuffer:
    """Async line reader that handles arbitrary line lengths.

    Reads in 8KB chunks until newline, solving asyncio StreamReader's
    64KB buffer limit for large NDJSON responses.
    """

    def __init__(self, stream: asyncio.StreamReader):
        self._stream = stream
        self._buf = b""

    async def readline(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = await self._stream.read(8192)
            if not chunk:
                line = self._buf
                self._buf = b""
                return line
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line + b"\n"


# ── ACP Client (JSON-RPC over stdio) ────────────────────────────────────────

@dataclass
class _Pending:
    fut: asyncio.Future


class ACPClient:
    """Minimal JSON-RPC 2.0 client over newline-delimited JSON on stdio.

    Used for Gemini ACP protocol. Handles:
    - Outgoing requests with ID tracking and futures
    - Incoming responses routed to pending futures
    - Incoming notifications dispatched to handlers
    - Incoming server-side requests answered by registered handlers
    """

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None):
        self._cmd = cmd
        self._env = env or os.environ.copy()
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._notif_handlers: dict[str, list[Callable]] = {}
        self._request_handlers: dict[str, Callable[[dict], Awaitable[dict]]] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def stop(self) -> None:
        try:
            for p in self._pending.values():
                if not p.fut.done():
                    p.fut.cancel()
            self._pending.clear()
            if self._proc and self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        finally:
            self._proc = None
            for task in (self._reader_task, self._stderr_task):
                if task:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            self._reader_task = None
            self._stderr_task = None

    def on_notification(self, method: str, handler: Callable) -> None:
        self._notif_handlers.setdefault(method, []).append(handler)

    def off_notification(self, method: str, handler: Callable | None = None) -> None:
        """Remove notification handler(s). If handler is None, remove all."""
        if method not in self._notif_handlers:
            return
        if handler is None:
            self._notif_handlers[method] = []
        else:
            self._notif_handlers[method] = [
                h for h in self._notif_handlers[method] if h != handler
            ]

    def on_request(self, method: str, handler: Callable[[dict], Awaitable[dict]]) -> None:
        self._request_handlers[method] = handler

    async def request(self, method: str, params: dict | None = None) -> Any:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACP process not started")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = _Pending(fut=fut)
        obj = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()
        return await fut

    async def _reader_loop(self) -> None:
        assert self._proc and self._proc.stdout
        reader = LineBuffer(self._proc.stdout)
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue

            # Response to our request
            if "id" in msg and "method" not in msg:
                slot = self._pending.pop(msg["id"], None)
                if slot:
                    if "error" in msg:
                        slot.fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        slot.fut.set_result(msg.get("result"))
                continue

            # Server-side request (needs response)
            if "method" in msg and "id" in msg:
                req_id = msg["id"]
                handler = self._request_handlers.get(msg["method"])
                if handler:
                    try:
                        result = await handler(msg.get("params") or {})
                        await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
                    except Exception as e:
                        await self._send({
                            "jsonrpc": "2.0", "id": req_id,
                            "error": {"code": -32000, "message": str(e)},
                        })
                else:
                    await self._send({
                        "jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32601, "message": "Method not found"},
                    })
                continue

            # Notification (no id)
            if "method" in msg and "id" not in msg:
                for h in self._notif_handlers.get(msg["method"], []):
                    try:
                        h(msg.get("params") or {})
                    except Exception:
                        pass

    async def _send(self, obj: dict) -> None:
        if self._proc and self._proc.stdin:
            self._proc.stdin.write((json.dumps(obj) + "\n").encode())
            await self._proc.stdin.drain()

    async def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break


# ── Gemini via ACP ───────────────────────────────────────────────────────────

async def _call_gemini(prompt: str) -> str:
    """Call Gemini CLI via ACP protocol. Per-call subprocess isolation."""
    env = os.environ.copy()
    env.setdefault("NO_BROWSER", "1")
    client = ACPClient(["gemini", "--acp"], env=env)

    # Auto-approve permission requests
    async def _handle_permission(params: dict) -> dict:
        options = params.get("options") or []
        chosen = None
        for kind in ("allow_always", "allow_once"):
            chosen = next((o for o in options if o.get("kind") == kind), None)
            if chosen:
                break
        if not chosen and options:
            chosen = options[0]
        if not chosen:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    async def _fs_noop(params: dict) -> dict:
        return {"content": ""} if "read" in str(params) else {}

    client.on_request("session/request_permission", _handle_permission)
    client.on_request("fs/read_text_file", _fs_noop)
    client.on_request("fs/write_text_file", _fs_noop)

    try:
        await client.start()

        # Initialize
        await client.request("initialize", {
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "protocolVersion": 1,
        })

        # Create session
        result = await client.request("session/new", {"cwd": os.getcwd(), "mcpServers": []})
        session_id = result.get("sessionId")
        if not session_id:
            raise RuntimeError("Failed to create Gemini session")

        # Collect response text via notification handler
        text_chunks: list[str] = []
        q: asyncio.Queue = asyncio.Queue()

        def _on_update(params: dict) -> None:
            if params.get("sessionId") != session_id:
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate") or update.get("type")
            if kind in ("agent_message_chunk", "agent_thought_chunk"):
                text = ((update.get("content") or {}).get("text")) or update.get("text") or ""
                if isinstance(text, str) and text:
                    text_chunks.append(text)
            q.put_nowait(update)

        client.on_notification("session/update", _on_update)

        # Send prompt
        prompt_task = asyncio.create_task(
            client.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt}],
            })
        )

        # Wait for prompt completion with timeout
        try:
            await asyncio.wait_for(prompt_task, timeout=GEMINI_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Gemini timed out after {GEMINI_TIMEOUT}s")

        # Drain any remaining notifications
        await asyncio.sleep(0.1)

        return "".join(text_chunks).strip() or "[No response from Gemini]"

    finally:
        await client.stop()


# ── Codex via App-Server ─────────────────────────────────────────────────────

async def _call_codex(prompt: str) -> str:
    """Call Codex CLI via app-server (JSON-RPC 2.0). Per-call subprocess isolation.

    Uses the same ACPClient as Gemini — both speak JSON-RPC over stdio.
    Protocol: initialize → thread/start → turn/start → item/* notifications → turn/completed
    """
    client = ACPClient(["codex", "app-server"])

    # Collect agent message deltas via notifications
    text_chunks: list[str] = []
    turn_done = asyncio.Event()
    turn_error: list[str] = []

    def _on_agent_delta(params: dict) -> None:
        delta = params.get("delta", "")
        if isinstance(delta, str) and delta:
            text_chunks.append(delta)

    def _on_turn_completed(params: dict) -> None:
        status = params.get("status", "")
        if status == "failed":
            turn_error.append(params.get("error", "Turn failed"))
        turn_done.set()

    client.on_notification("item/agentMessage/delta", _on_agent_delta)
    client.on_notification("turn/completed", _on_turn_completed)

    try:
        await client.start()

        # 1. Initialize handshake
        await client.request("initialize", {
            "clientInfo": {
                "name": "ai_commander",
                "title": "AI Commander MCP",
                "version": "0.1.0",
            },
            "capabilities": {},
        })

        # 2. Send initialized notification (required by protocol)
        if client._proc and client._proc.stdin:
            notif = json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n"
            client._proc.stdin.write(notif.encode())
            await client._proc.stdin.drain()

        # 3. Create thread with auto-approval
        thread_result = await client.request("thread/start", {
            "cwd": os.getcwd(),
            "approvalPolicy": "never",
            "sandbox": "full-access",
        })
        thread_id = thread_result.get("thread", {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex app-server: failed to create thread")

        # 4. Start turn with user prompt
        turn_done.clear()
        await client.request("turn/start", {
            "threadId": thread_id,
            "input": prompt,
        })

        # 5. Wait for turn/completed notification
        try:
            await asyncio.wait_for(turn_done.wait(), timeout=CODEX_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Codex timed out after {CODEX_TIMEOUT}s")

        if turn_error:
            raise RuntimeError(f"Codex error: {turn_error[0]}")

        return "".join(text_chunks).strip() or "[No response from Codex]"

    finally:
        await client.stop()


# ── FastMCP Server ───────────────────────────────────────────────────────────

mcp = FastMCP("ai-commander")


@mcp.tool()
async def ask_gemini(prompt: str) -> str:
    """Send a prompt to Gemini (gemini-2.5-pro) and return its response."""
    try:
        async with _gemini_sem:
            return await _call_gemini(prompt)
    except Exception as exc:
        return f"[ERROR] {type(exc).__name__}: {exc}"


@mcp.tool()
async def ask_codex(prompt: str) -> str:
    """Send a prompt to Codex (gpt-5.4) and return its response."""
    try:
        async with _codex_sem:
            return await _call_codex(prompt)
    except Exception as exc:
        return f"[ERROR] {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    mcp.run()
