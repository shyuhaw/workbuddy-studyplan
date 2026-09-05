# -*- coding: utf-8 -*-
"""
MCP Client 联调脚本（JD #6：MCP - Model Context Protocol）
========================================================
演示如何作为 MCP 客户端调用服务端的工具。

用法：
    python src/mcp_client.py          # 默认 demo 模式
    python src/mcp_client.py --stdio  # stdio 模式（连接真实 MCP server）

作者：麦当 · 2026-09-04
"""

import json
import sys
import os
import subprocess
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 客户端（MCP 基于此）
# ---------------------------------------------------------------------------
class MCPClient:
    """最小可用 MCP Client —— 支持 stdio 传输的 JSON-RPC 2.0 通信。

    与 MCP SDK 的对应：
        MCPClient          <->  MCPClient (std)
        .initialize()      <-- 等价于 SDK 的 client.connect()
        .list_tools()      <-- 等价于 SDK 的 client.list_tools()
        .call_tool()       <-- 等价于 SDK 的 client.call_tool()
    """

    def __init__(self, server_cmd: list, timeout: int = 30):
        self.server_cmd = server_cmd
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._msg_id = 0
        self._server_info: dict = {}
        self._tool_cache: list = []

    def _next_id(self) -> str:
        self._msg_id += 1
        return str(self._msg_id)

    def _send(self, method: str, params: dict = None) -> dict:
        """发送 JSON-RPC 请求并等待响应。"""
        msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

        # 读取响应（MCP 协议：每行一个 JSON）
        start = time.time()
        while time.time() - start < self.timeout:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server 无响应（连接已关闭）")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 只返回匹配 id 的响应
            if resp.get("id") == msg["id"]:
                return resp
        raise TimeoutError(f"MCP 请求超时 ({self.timeout}s)")

    def initialize(self) -> dict:
        """初始化握手。"""
        resp = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "test-client", "version": "0.1.0"},
        })
        result = resp.get("result", {})
        self._server_info = result.get("serverInfo", {})
        return result

    def list_tools(self) -> list:
        """列出所有可用工具。"""
        resp = self._send("tools/list")
        tools = resp.get("result", {}).get("tools", [])
        self._tool_cache = tools
        return tools

    def call_tool(self, name: str, arguments: dict = None) -> dict:
        """调用工具。"""
        resp = self._send("tools/call", {"name": name, "arguments": arguments or {}})
        result = resp.get("result", {})
        content = result.get("content", [])
        # 提取文本内容
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return {
            "content": content,
            "text": "\n".join(texts),
            "isError": result.get("isError", False),
        }

    def start(self):
        """启动 MCP server 子进程。"""
        self._proc = subprocess.Popen(
            self.server_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line buffered
        )
        # 等待 server 初始化通知
        start = time.time()
        while time.time() - start < self.timeout:
            line = self._proc.stdout.readline()
            if not line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("method") == "notifications/initialized":
                    break
            except json.JSONDecodeError:
                continue

    def stop(self):
        """停止 MCP server。"""
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=5)
            self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


# ---------------------------------------------------------------------------
# Demo：不启动真实 server，直接调用已注册的 handler
# ---------------------------------------------------------------------------
def demo_direct():
    """直接调用方式（不依赖子进程，适合快速验证）。"""
    print("=" * 60)
    print("MCP Client 联调演示（直接调用模式）")
    print("=" * 60)

    # 导入 server 模块
    from mcp_server import MCPServer, make_builtin_handlers, _rpc_request, _rpc_response

    # 创建 server
    server = MCPServer(name="外贸邮件-agent-tools", version="0.1.0")
    handlers = make_builtin_handlers()

    # 注册工具
    for name, h in handlers.items():
        server.register_tool(
            name=name,
            description=f"MCP 包装工具：{name}",
            input_schema={"type": "object", "properties": {}},
            handler=h,
        )

    # 模拟 MCP 协议调用
    msg_id = "test-001"

    # 1. initialize
    init_msg = _rpc_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
    })
    init_resp = server._dispatch(init_msg)
    print("\n[1] initialize 响应:")
    print(json.dumps(init_resp, ensure_ascii=False, indent=2))

    # 2. tools/list
    list_msg = _rpc_request("tools/list")
    list_resp = server._dispatch(list_msg)
    tools = list_resp.get("result", {}).get("tools", [])
    print(f"\n[2] 可用工具数量: {len(tools)}")
    for t in tools:
        print(f"   - {t['name']}: {t['description'][:50]}...")

    # 3. call_tool（以 retrieve_history 为例）
    call_msg = _rpc_request("tools/call", {
        "name": "retrieve_history",
        "arguments": {"query": "Global Import Ltd LED panel light"},
    })
    call_resp = server._dispatch(call_msg)
    content = call_resp.get("result", {}).get("content", [])
    text = content[0].get("text", "") if content else ""
    print(f"\n[3] retrieve_history 调用结果:")
    print(f"   文本长度: {len(text)} 字符")
    print(f"   前100字符: {text[:100]}...")

    # 4. call_tool（simulate_slow 测试超时处理）
    call_msg2 = _rpc_request("tools/call", {
        "name": "simulate_slow",
        "arguments": {"seconds": 0.1},
    })
    call_resp2 = server._dispatch(call_msg2)
    print(f"\n[4] simulate_slow 调用结果:")
    print(json.dumps(call_resp2, ensure_ascii=False, indent=2))

    print("\n✅ 直接调用模式联调完成")


# ---------------------------------------------------------------------------
# Demo：子进程模式（模拟真实 MCP 客户端）
# ---------------------------------------------------------------------------
def demo_subprocess():
    """通过子进程调用 MCP server（更接近真实场景）。"""
    print("=" * 60)
    print("MCP Client 联调演示（子进程模式）")
    print("=" * 60)

    server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")

    with MCPClient([sys.executable, server_path, "--demo"]) as client:
        # 1. initialize
        print("\n[1] 初始化握手...")
        init_result = client.initialize()
        print(f"   Server: {init_result.get('serverInfo', {}).get('name')} v{init_result.get('serverInfo', {}).get('version')}")
        print(f"   Capabilities: {list(init_result.get('capabilities', {}).keys())}")

        # 2. list_tools
        print("\n[2] 列出工具...")
        tools = client.list_tools()
        print(f"   可用工具数量: {len(tools)}")
        for t in tools:
            print(f"   - {t['name']}")

        # 3. call_tool
        print("\n[3] 调用 retrieve_history...")
        result = client.call_tool("retrieve_history", {
            "query": "Global Import Ltd LED panel light"
        })
        print(f"   返回文本长度: {len(result['text'])} 字符")
        print(f"   前100字符: {result['text'][:100]}...")
        print(f"   isError: {result['isError']}")

    print("\n✅ 子进程模式联调完成")


# ---------------------------------------------------------------------------
# 集成测试：MCP Client 与 AgentLoop 对接
# ---------------------------------------------------------------------------
def demo_integration():
    """演示 MCP 工具如何集成到 Agent 中。"""
    print("=" * 60)
    print("MCP 集成测试（MCP 工具 → Agent 调用）")
    print("=" * 60)

    from mcp_server import MCPServer, make_builtin_handlers, _rpc_request

    # 创建 server
    server = MCPServer(name="外贸邮件-agent-tools", version="0.1.0")
    handlers = make_builtin_handlers()
    for name, h in handlers.items():
        server.register_tool(name, f"MCP: {name}", {"type": "object", "properties": {}}, h)

    # 模拟 Agent 调用 MCP 工具
    test_cases = [
        ("retrieve_history", {"query": "Canada Decor Inc acoustic ceiling panel"}),
        ("classify_email", {"subject": "Order inquiry", "body": "We would like to place an order"}),
    ]

    for tool_name, args in test_cases:
        msg = _rpc_request("tools/call", {"name": tool_name, "arguments": args})
        resp = server._dispatch(msg)
        content = resp.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else ""
        print(f"\n调用 {tool_name}:")
        print(f"  返回长度: {len(text)} 字符")
        print(f"  前80字符: {text[:80]}...")

    print("\n✅ MCP 集成测试完成")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="MCP Client 联调脚本")
    ap.add_argument("--mode", choices=["direct", "subprocess", "integration"],
                    default="direct", help="演示模式")
    args = ap.parse_args()

    if args.mode == "direct":
        demo_direct()
    elif args.mode == "subprocess":
        demo_subprocess()
    elif args.mode == "integration":
        demo_integration()


if __name__ == "__main__":
    main()
