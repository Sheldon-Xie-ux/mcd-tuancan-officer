#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦当劳 MCP Server 最小客户端（Streamable HTTP / JSON-RPC 2.0）

仅依赖 Python 标准库，任何装了 Python 3.8+ 的机器都能跑。

用法：
    export MCD_MCP_TOKEN="你的 MCP Token"
    python3 mcd_client.py tools                     # 列出全部工具
    python3 mcd_client.py call <tool> '<json参数>'   # 调用单个工具

也作为库使用：
    from mcd_client import MCPClient
    mcp = MCPClient()
    mcp.initialize()
    print(mcp.list_tools())
    print(mcp.call_tool("query-nearby-stores", {"address": "无锡市梁溪区"}))
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    """MCP 调用失败，带上服务端原始信息，方便排查。"""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _ssl_context():
    ctx = ssl.create_default_context()
    return ctx


class MCPClient:
    def __init__(self, token=None, endpoint=None, timeout=30, verbose=False):
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self.token:
            raise MCPError(
                "缺少 MCP Token。请先设置环境变量 MCD_MCP_TOKEN，"
                "或在 https://open.mcd.cn/mcp 用手机号登录后到「控制台」激活领取。"
            )
        self.endpoint = (endpoint or os.environ.get("MCD_MCP_URL") or DEFAULT_ENDPOINT).rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        self.session_id = None
        self._next_id = 0
        self._initialized = False

    # ---------- 底层传输 ----------

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "mcd-tuancan-officer/1.0",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body, headers=self._headers(), method="POST"
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            hint = ""
            if e.code == 401:
                hint = "（MCP Token 无效、已过期或未激活，请到 open.mcd.cn/mcp 重新领取）"
            elif e.code == 429:
                hint = "（触发限流：每 Token 每分钟最多 600 次请求）"
            raise MCPError(
                "HTTP %s %s%s" % (e.code, raw[:400], hint), status=e.code, payload=raw
            ) from None
        except urllib.error.URLError as e:
            raise MCPError("网络不可达：%s（检查网络或尝试关闭代理）" % e.reason) from None

        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        ctype = (resp.headers.get("Content-Type") or "").lower()
        raw = resp.read().decode("utf-8", "replace")

        if not raw.strip():
            return None
        if "text/event-stream" in ctype:
            return self._parse_sse(raw, payload.get("id"))
        return self._parse_json(raw, payload.get("id"))

    @staticmethod
    def _parse_json(raw, want_id):
        try:
            data = json.loads(raw)
        except ValueError:
            raise MCPError("响应不是合法 JSON：%s" % raw[:400]) from None
        if isinstance(data, dict) and data.get("id") == want_id:
            return data
        if isinstance(data, dict) and "result" in data:
            return data
        return data

    @staticmethod
    def _parse_sse(raw, want_id):
        """从 SSE 流里挑出 id 匹配的那条 JSON-RPC 响应。"""
        picked = None
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                msg = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
            if picked is None and isinstance(msg, dict) and ("result" in msg or "error" in msg):
                picked = msg
        if picked is None:
            raise MCPError("SSE 流中没有找到响应：%s" % raw[:400])
        return picked

    def _rpc(self, method, params=None, notify=False):
        self._next_id += 1
        rid = self._next_id
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = rid
        else:
            payload.pop("id", None)

        msg = self._post(payload)
        if notify:
            return None
        if msg is None:
            raise MCPError("%s 返回空响应" % method)
        if "error" in msg and msg["error"]:
            err = msg["error"]
            raise MCPError(
                "%s 失败：%s %s" % (method, err.get("code"), err.get("message")),
                payload=err,
            )
        return msg.get("result")

    # ---------- 协议层 ----------

    def initialize(self):
        if self._initialized:
            return self
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": {"name": "mcd-tuancan-officer", "version": "1.0.0"},
            },
        )
        if self.verbose:
            print("[mcp] initialized: %s" % json.dumps(result, ensure_ascii=False)[:300],
                  file=sys.stderr)
        self._rpc("notifications/initialized", {}, notify=True)
        self._initialized = True
        return self

    def list_tools(self):
        self.initialize()
        result = self._rpc("tools/list", {})
        return (result or {}).get("tools", [])

    def call_tool(self, name, arguments=None):
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return result

    def call_tool_data(self, name, arguments=None):
        """调用工具并把 MCP 的 content 块解析成 Python 对象。"""
        result = self.call_tool(name, arguments)
        return decode_content(result)


def decode_content(result):
    """把 tools/call 的返回拆开：优先返回结构化 JSON，否则返回文本。"""
    if not result:
        return None
    if result.get("isError"):
        raise MCPError("工具返回错误：%s" % json.dumps(result, ensure_ascii=False)[:400])
    blocks = result.get("content") or []
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            return texts[0]
    if texts:
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined
    return result


def _cli(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    try:
        mcp = MCPClient(verbose=True)
        if cmd == "tools":
            tools = mcp.list_tools()
            print(json.dumps(tools, ensure_ascii=False, indent=2))
            print("\n共 %d 个工具" % len(tools), file=sys.stderr)
            return 0
        if cmd == "call":
            if len(argv) < 3:
                print("用法：mcd_client.py call <tool> '<json参数>'", file=sys.stderr)
                return 2
            args = json.loads(argv[3]) if len(argv) > 3 else {}
            out = mcp.call_tool_data(argv[2], args)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        print("未知命令：%s" % cmd, file=sys.stderr)
        return 2
    except MCPError as e:
        print("MCP 错误：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
