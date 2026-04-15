"""
ai-commander: thin MCP server bridging Claude to Gemini and Codex.

Exposes two tools:
  - ask_gemini(prompt) → Gemini via ACP protocol (JSON-RPC over stdio)
  - ask_codex(prompt)  → Codex via app-server protocol (JSON-RPC over stdio)

Architecture copied from Roundtable (claudable_helper/cli/adapters).
Both providers use a shared _ACPClient (JSON-RPC 2.0 over NDJSON/stdio).
Per-call subprocess isolation — each tool call spawns a fresh process.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

# ── Configuration ────────────────────────────────────────────────────────────

GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "180"))
CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "300"))
GEMINI_MAX_CONCURRENT = int(os.getenv("GEMINI_MAX_CONCURRENT", "4"))
CODEX_MAX_CONCURRENT = int(os.getenv("CODEX_MAX_CONCURRENT", "4"))

_gemini_sem = asyncio.Semaphore(GEMINI_MAX_CONCURRENT)
_codex_sem = asyncio.Semaphore(CODEX_MAX_CONCURRENT)


# ── LineBuffer (from Roundtable cli/base.py) ─────────────────────────────────

class LineBuffer:
    """Async line reader that handles arbitrary line lengths.

    Reads in 8KB chunks until newline, solving asyncio StreamReader's
    64KB buffer limit for large NDJSON responses.
    """

    def __init__(self, stream: asyncio.StreamReader):
        self.stream = stream
        self.buffer = b""

    async def readline(self) -> bytes:
        while b"\n" not in self.buffer:
            chunk = await self.stream.read(8192)
            if not chunk:
                line = self.buffer
                self.buffer = b""
                return line
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line + b"\n"


# ── _ACPClient (from Roundtable cli/adapters/qwen_cli.py) ───────────────────

@dataclass
class _Pending:
    fut: asyncio.Future


class _ACPClient:
    """Minimal JSON-RPC 2.0 client over newline-delimited JSON on stdio.

    Handles:
    - Outgoing requests with ID tracking and futures
    - Incoming responses routed to pending futures
    - Incoming notifications dispatched to handlers
    - Incoming server-side requests answered by registered handlers
    """

    def __init__(self, cmd: List[str], env: Optional[Dict[str, str]] = None):
        self._cmd = cmd
        self._env = env or os.environ.copy()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: Dict[int, _Pending] = {}
        self._notif_handlers: Dict[str, List[Callable]] = {}
        self._request_handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

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
            for pending in self._pending.values():
                if not pending.fut.done():
                    pending.fut.cancel()
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
            if self._reader_task:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except asyncio.CancelledError:
                    pass
                self._reader_task = None
            if self._stderr_task:
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except asyncio.CancelledError:
                    pass
                self._stderr_task = None

    def on_notification(self, method: str, handler: Callable) -> None:
        self._notif_handlers.setdefault(method, []).append(handler)

    def off_notification(self, method: str, handler: Optional[Callable] = None) -> None:
        if method not in self._notif_handlers:
            return
        if handler is None:
            self._notif_handlers[method] = []
        else:
            self._notif_handlers[method] = [
                h for h in self._notif_handlers[method] if h != handler
            ]

    def on_request(self, method: str, handler: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]) -> None:
        self._request_handlers[method] = handler

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACP process not started")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = _Pending(fut=fut)
        obj = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        data = (json.dumps(obj) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        return await fut

    async def send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACP process not started")
        obj = {"jsonrpc": "2.0", "method": method}
        if params:
            obj["params"] = params
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _reader_loop(self) -> None:
        try:
            if not self._proc or not self._proc.stdout:
                return
            reader = LineBuffer(self._proc.stdout)
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:
                    continue

                if not isinstance(msg, dict):
                    continue

                # Response to our request
                if "id" in msg and "method" not in msg:
                    slot = self._pending.pop(int(msg["id"]), None) if isinstance(msg.get("id"), (int, str)) else None
                    if not slot:
                        continue
                    if "error" in msg:
                        slot.fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        slot.fut.set_result(msg.get("result"))
                    continue

                # Request from agent (client-side)
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

                # Notification from agent
                if "method" in msg and "id" not in msg:
                    for h in self._notif_handlers.get(msg["method"], []):
                        try:
                            h(msg.get("params") or {})
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _send(self, obj: Dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            return
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _stderr_loop(self) -> None:
        """Background task to drain stderr to prevent blocking."""
        try:
            if not self._proc or not self._proc.stderr:
                return
            reader = LineBuffer(self._proc.stderr)
            while True:
                line = await reader.readline()
                if not line:
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


# ── Gemini via ACP (from Roundtable cli/adapters/gemini_cli.py) ──────────────

async def _call_gemini(prompt: str) -> str:
    """Call Gemini CLI via ACP protocol. Per-call subprocess isolation."""
    env = os.environ.copy()
    env.setdefault("NO_BROWSER", "1")
    client = _ACPClient(["gemini", "--acp"], env=env)

    # Auto-approve permission requests (from Roundtable)
    async def _handle_permission(params: Dict[str, Any]) -> Dict[str, Any]:
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

    async def _fs_read(params: Dict[str, Any]) -> Dict[str, Any]:
        return {"content": ""}

    async def _fs_write(params: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    client.on_request("session/request_permission", _handle_permission)
    client.on_request("fs/read_text_file", _fs_read)
    client.on_request("fs/write_text_file", _fs_write)

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

        # Collect response text via notification handler (from Roundtable)
        text_chunks: List[str] = []

        def _on_update(params: Dict[str, Any]) -> None:
            if params.get("sessionId") != session_id:
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate") or update.get("type")
            if kind in ("agent_message_chunk", "agent_thought_chunk"):
                text = ((update.get("content") or {}).get("text")) or update.get("text") or ""
                if isinstance(text, str) and text:
                    text_chunks.append(text)

        client.on_notification("session/update", _on_update)
        try:
            # Send prompt and wait (from Roundtable)
            try:
                await asyncio.wait_for(
                    client.request("session/prompt", {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    }),
                    timeout=GEMINI_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Gemini timed out after {GEMINI_TIMEOUT}s")

            await asyncio.sleep(0.1)
            return "".join(text_chunks).strip() or "[No response from Gemini]"
        finally:
            client.off_notification("session/update", _on_update)
    finally:
        await client.stop()


# ── Codex via App-Server ─────────────────────────────────────────────────────

async def _call_codex(prompt: str) -> str:
    """Call Codex CLI via app-server (JSON-RPC 2.0). Per-call subprocess isolation.

    Protocol: initialize → initialized → thread/start → turn/start →
              item/agentMessage/delta notifications → turn/completed
    """
    client = _ACPClient(["codex", "app-server"])

    text_chunks: List[str] = []
    turn_done = asyncio.Event()
    turn_error: List[str] = []

    def _on_agent_delta(params: Dict[str, Any]) -> None:
        delta = params.get("delta", "")
        if isinstance(delta, str) and delta:
            text_chunks.append(delta)

    def _on_turn_completed(params: Dict[str, Any]) -> None:
        turn = params.get("turn") or {}
        status = turn.get("status", "")
        if status == "failed":
            turn_error.append(turn.get("error", "Turn failed"))
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
        await client.send_notification("initialized")

        # 3. Create thread with auto-approval
        thread_result = await client.request("thread/start", {
            "cwd": os.getcwd(),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        })
        thread_id = (thread_result or {}).get("thread", {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex app-server: failed to create thread")

        # 4. Start turn with user prompt
        turn_done.clear()
        await client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
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
