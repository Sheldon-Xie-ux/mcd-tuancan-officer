#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
探测麦当劳 MCP Server 的真实工具清单与参数结构，落盘到 docs/mcp-tools.json。

跑法：
    export MCD_MCP_TOKEN="..."
    python3 scripts/probe.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_client import MCPClient, MCPError  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "mcp-tools.json")


def main():
    mcp = MCPClient(verbose=True)
    tools = mcp.list_tools()
    tools = sorted(tools, key=lambda t: t.get("name", ""))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(tools, f, ensure_ascii=False, indent=2)

    print("共 %d 个工具 -> %s\n" % (len(tools), OUT))
    for t in tools:
        schema = t.get("inputSchema") or {}
        props = list((schema.get("properties") or {}).keys())
        required = schema.get("required") or []
        print("- %-28s %s" % (t.get("name"), t.get("title") or ""))
        if props:
            print("    params: %s" % ", ".join(
                p + ("*" if p in required else "") for p in props))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MCPError as e:
        print("MCP 错误：%s" % e, file=sys.stderr)
        sys.exit(1)
