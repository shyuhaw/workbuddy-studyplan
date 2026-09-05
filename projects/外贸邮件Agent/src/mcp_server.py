# -*- coding: utf-8 -*-
"""
轻量 MCP 服务器（JD #6：MCP - Model Context Protocol）
================================================

为什么不用官方 mcp Python SDK：
- 本机红线：不装重依赖，venv 创建失败、pip --target 装 mcp 会拉 torch/numpy 链
- MCP 协议核心本质是 **JSON-RPC 2.0 over stdio / SSE**，几十行就能实现一个最小可用子集
- JD #6 考的是「能用 MCP 暴露工具」不是「会 pip install mcp」

本文件实现 MCP 1.0 协议的最小可用子集：
- transport: stdout/stderr（stdin 读 JSON-RPC，stdout 写 JSON-RPC），适合 CLI 场景
- capabilities: 仅实现「tools」，不实现 resources/prompts（够用）
- JSON-RPC 2.0 报文格式严格遵循规范

MCP 核心概念（面试要讲清）：
    - Server：暴露工具 / 资源 / 提示词的服务端
    - Client：调用 Server 工具/资源的客户端（本框架内就是 AgentLoop）
    - Transport：stdio / SSE / HTTP，协议透明
    - Tools：JSON Schema 描述的函数，MCP 客户端按 schema 组装 tool_calls

作者：麦当 · 2026-09-04（Day06 续 · JD #6）
"""

import json
import sys
import os
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 基础（MCP 基于此）
# ---------------------------------------------------------------------------
def _rpc_request(method: str, params: dict = None, msg_id: str = None) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id or str(int(time.time() * 1000)),
            "method": method, "params": params or {}}


def _rpc_response(result: Any, msg_id: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(code: int, message: str, msg_id: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# MCP 协议层：Server 核心
# ---------------------------------------------------------------------------
class MCPServer:
    """最小可用 MCP Server —— 支持工具注册 + 按 MCP 协议响应。

    与完整 mcp Python SDK 的对应：
        MCP server  <->  MCPClient (std)
        .register_tool()       <-- 等价于 SDK 的 @server.tool()
        .run_sync()            <-- 等价于 SDK 的 `asyncio.run(server.run())`
    """

    def __init__(self, name: str, version: str = "0.1.0"):
        self.name = name
        self.version = version
        self._tools: Dict[str, dict] = {}   # name -> {schema, handler}
        self._resources: Dict[str, dict] = {}
        self._prompts: Dict[str, dict] = {}

    def register_tool(self, name: str, description: str, input_schema: dict,
                      handler):
        """注册一个工具。handler(tool_call: dict) -> 返回字符串或 dict（自动序列化）。"""
        if name in self._tools:
            raise ValueError(f"工具 {name!r} 已注册")
        self._tools[name] = {
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
        }
        return self

    # ---- MCP 协议处理 ----
    def _handle_initialize(self, msg_id: str, params: dict) -> dict:
        """初始化握手：返回 server capabilities + client capabilities（仅声明支持）。"""
        caps = {"capabilities": {
            "tools": {},   # 有 tools 就亮出来
            "resources": {"subscribe": False} if self._resources else None,
        }}
        return _rpc_response({
            "protocolVersion": "2024-11-05",  # MCP 1.0 稳定版
            "capabilities": caps["capabilities"],
            "serverInfo": {"name": self.name, "version": self.version},
        }, msg_id)

    def _handle_list_tools(self, msg_id: str) -> dict:
        tools = []
        for name, t in self._tools.items():
            tools.append({
                "name": name,
                "description": t["description"],
                "inputSchema": t["input_schema"],
            })
        return _rpc_response({"tools": tools}, msg_id)

    def _handle_call_tool(self, msg_id: str, params: dict) -> dict:
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        if tool_name not in self._tools:
            return _rpc_error(-32602, f"未知工具：{tool_name}", msg_id)
        try:
            raw = self._tools[tool_name]["handler"](args)
            # 返回格式 MCP 要求是 ContentBlock 列表；字符串包装成 text
            if isinstance(raw, (dict, list)):
                content = [{"type": "text", "text": json.dumps(raw, ensure_ascii=False)}]
            else:
                content = [{"type": "text", "text": str(raw)}]
            return _rpc_response({"content": content, "isError": False}, msg_id)
        except Exception as e:
            return _rpc_error(-32000, f"{type(e).__name__}: {e}", msg_id)

    def _handle_unknown_method(self, msg_id: str, method: str) -> dict:
        return _rpc_error(-32601, f"不支持的方法：{method}", msg_id)

    def _dispatch(self, msg: dict) -> Optional[dict]:
        """按 JSON-RPC 2.0 分发，返回响应（或 None 表示 consume 但不回）。"""
        rid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            return self._handle_initialize(rid, params)
        if method == "tools/list":
            return self._handle_list_tools(rid)
        if method == "tools/call":
            return self._handle_call_tool(rid, params)
        return self._handle_unknown_method(rid, method)

    def run_sync(self):
        """stdio 模式运行（等价于 SDK 的 server.run()，但同步版，兼容 mock）。

        用法：python src/mcp_server.py
        输入：从 stdin 逐行读 JSON-RPC 报文
        输出：往 stdout 写 JSON-RPC 响应
        """
        sys.stdout.write(json.dumps(_rpc_request("notifications/initialized")) + "\n")
        sys.stdout.flush()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as e:
                print(json.dumps(_rpc_error(-32700, f"JSON 解析失败: {e}", None)),
                      file=sys.stdout, flush=True)
                continue
            resp = self._dispatch(msg)
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False), file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# 内置工具：与 Agent 现有工具对齐（方便对接）
# ---------------------------------------------------------------------------
def make_builtin_handlers(session=None):
    """把项目现有工具（classify_email / retrieve_history / ...）包装成 MCP handlers。

    session: ToolSession 实例（由客户端在初始化后注入）
    """
    try:
        from tools import execute_tool, make_tools, ToolSession
    except Exception:
        session = None

    if session is None:
        session = ToolSession({}) if ToolSession is not None else None

    def _wrap(name):
        def handler(args):
            impl = {}
            spec, impl = make_tools(session)
            out = execute_tool(name, args, impl, session)
            return json.loads(out)
        return handler

    handlers = {
        "classify_email": _wrap("classify_email"),
        "extract_fields": _wrap("extract_fields"),
        "retrieve_history": _wrap("retrieve_history"),
        "build_context": _wrap("build_context"),
        "generate_answer": _wrap("generate_answer"),
        "finish": _wrap("finish"),
        "simulate_slow": lambda a: time.sleep(float(a.get("seconds", 1))) or {"ok": True, "sleep_sec": a.get("seconds")},
    }
    return handlers


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = __import__("argparse").ArgumentParser(description="MCP Server（最小可用子集）")
    ap.add_argument("--demo", action="store_true", help="非 stdio 模式：打工具清单后退出（供调试）")
    args = ap.parse_args()

    server = MCPServer(name="外贸邮件-agent-tools", version="0.1.0")
    handlers = make_builtin_handlers()
    for name, h in handlers.items():
        server.register_tool(
            name=name,
            description=f"MCP 包装工具：{name}，透传给底层 tools.py 执行",
            input_schema={"type": "object", "properties": {"__dummy": {"type": "string"}}},
            handler=h,
        )

    if args.demo:
        # 非 stdio 模式：打印 MCP 握手 + 工具清单（调试用）
        import io
        class FakeStdout:
            def __init__(self):
                self.buf = []
            def write(self, s):
                self.buf.append(s.strip())
            def flush(self):
                pass
        fake_out = FakeStdout()
        import builtins
        real_print = builtins.print
        builtins.print = lambda *a, **kw: fake_out.write(" ".join(str(x) for x in a))

        # 模拟 initialize handshake
        init_msg = _rpc_request("initialize", {"clientInfo": {"name": "test", "version": "0"}})
        init_resp = server._dispatch(init_msg)
        print(json.dumps(init_resp, ensure_ascii=False, indent=2))

        # 模拟 tools/list
        list_msg = _rpc_request("tools/list")
        list_resp = server._dispatch(list_msg)
        print(json.dumps(list_resp, ensure_ascii=False, indent=2))

        builtins.print = real_print
        print("\n✅ MCP 握手 + 工具清单打印完毕。真实 stdio 模式下：python src/mcp_server.py")
        return

    # stdio 模式：进入 MCP 协议循环
    server.run_sync()


if __name__ == "__main__":
    main()
