"""ACPClient 测试：用一个内嵌的 Python 假 peer 走完整 JSON-RPC 流程。"""
from __future__ import annotations

import asyncio
import sys

import pytest

from ai_commander.acp import ACPClient

# 假 ACP peer：从 stdin 读 NDJSON，按 method 应答。
# - ping      → 正常响应
# - big       → 响应带 200KB 数据（验证 64KB 行上限已解除）
# - notify_me → 先发一条通知，再响应
# - ask_back  → 反向向客户端发请求，等客户端回复后再响应原请求
# - err       → 返回 JSON-RPC error
# - never     → 不响应（验证超时和进程退出时的 pending 清理）
# - quit      → 退出进程
PEER_SCRIPT = r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

pending_ask = None
while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    if "method" not in msg:
        # 来自客户端的响应（用于 ask_back）
        if pending_ask is not None and msg.get("id") == 999:
            send({"jsonrpc": "2.0", "id": pending_ask, "result": {"peer_got": msg.get("result")}})
            pending_ask = None
        continue
    m = msg["method"]
    if m == "ping":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"pong": True}})
    elif m == "big":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"data": "x" * 200000}})
    elif m == "notify_me":
        send({"jsonrpc": "2.0", "method": "hello", "params": {"n": 1}})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
    elif m == "ask_back":
        pending_ask = msg["id"]
        send({"jsonrpc": "2.0", "id": 999, "method": "client/question", "params": {"q": "?"}})
    elif m == "err":
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": 1, "message": "boom"}})
    elif m == "never":
        pass
    elif m == "quit":
        break
"""


@pytest.fixture
async def client():
    c = ACPClient([sys.executable, "-u", "-c", PEER_SCRIPT])
    await c.start()
    yield c
    await c.stop()


async def test_request_response(client):
    result = await client.request("ping", timeout=10)
    assert result == {"pong": True}


async def test_large_message_survives(client):
    """一条 NDJSON 消息超过 asyncio 默认 64KB 行上限时，reader 不能崩溃。"""
    result = await client.request("big", timeout=10)
    assert len(result["data"]) == 200000
    # reader loop 仍然存活
    assert (await client.request("ping", timeout=10)) == {"pong": True}


async def test_error_response(client):
    with pytest.raises(RuntimeError, match="boom"):
        await client.request("err", timeout=10)


async def test_notification_dispatch(client):
    got: list[dict] = []
    client.on_notification("hello", got.append)
    await client.request("notify_me", timeout=10)
    assert got == [{"n": 1}]


async def test_peer_request_handled(client):
    """peer 反向请求由我们的 handler 响应，且不阻塞 reader。"""

    async def answer(params: dict) -> dict:
        return {"answer": 42}

    client.on_request("client/question", answer)
    result = await client.request("ask_back", timeout=10)
    assert result == {"peer_got": {"answer": 42}}


async def test_peer_request_without_handler(client):
    """未注册 handler 的反向请求应收到 Method not found，而不是挂起。"""
    task = asyncio.create_task(client.request("ask_back", timeout=5))
    result = await task
    # peer 把我们的 error 响应当作 result=None 转发回来
    assert result == {"peer_got": None}


async def test_request_timeout(client):
    with pytest.raises(asyncio.TimeoutError):
        await client.request("never", timeout=0.5)
    # 超时后 pending 已清理，客户端仍可用
    assert (await client.request("ping", timeout=10)) == {"pong": True}


async def test_pending_fails_on_peer_exit(client):
    task = asyncio.create_task(client.request("never"))
    await asyncio.sleep(0.2)
    await client.notify("quit")
    with pytest.raises(RuntimeError, match="意外退出"):
        await asyncio.wait_for(task, timeout=5)


async def test_request_before_start():
    c = ACPClient([sys.executable, "-c", "pass"])
    with pytest.raises(RuntimeError, match="未启动"):
        await c.request("ping")
