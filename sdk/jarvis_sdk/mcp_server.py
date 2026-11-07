"""JARVIS as an MCP server: any app that speaks the Model Context Protocol (Claude Desktop, VS Code, Cursor...) can use it.

It runs over stdin and stdout, started by that app, so it opens no port and needs no setup beyond one line in the app's
settings. Requests are marked as coming from another AI, so every action waits for a yes in JARVIS, whatever Full
control says. It talks to the running JARVIS and holds nothing itself."""
from __future__ import annotations

import json
import sys
from typing import Any

from . import Jarvis, JarvisError, __version__

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
TOOLS: list[dict[str, Any]] = [
    {"name": "ask_jarvis", "description": "Ask JARVIS, the user's personal assistant on their Windows PC, a question or to do something. "
     "Anything that changes something waits for the user to approve it in JARVIS.",
     "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
    {"name": "search_jarvis", "description": "Search the user's memories, past conversations, calendar, stored documents and connected apps.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}, "annotations": {"readOnlyHint": True}},
    {"name": "jarvis_status", "description": "Whether JARVIS is running and which apps are connected.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
]


def call_tool(name: str, args: dict[str, Any], jarvis: Jarvis | None = None) -> dict[str, Any]:
    try:
        j = jarvis or Jarvis()
        if name == "ask_jarvis":
            text = str(j.ask_full(str(args.get("message") or ""), outside=True).get("result") or "")
        elif name == "search_jarvis":
            text = json.dumps(j.search(str(args.get("query") or ""), int(args.get("limit") or 5)), ensure_ascii=False)
        elif name == "jarvis_status":
            text = json.dumps({"health": j.health(), "apps": [x["id"] for x in j.apps() if x["connected"]]})
        else:
            return {"content": [{"type": "text", "text": f"There's no tool called {name}."}], "isError": True}
    except JarvisError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    return {"content": [{"type": "text", "text": text}]}


def handle(msg: dict[str, Any], jarvis: Jarvis | None = None) -> dict[str, Any] | None:
    """The answer to one JSON-RPC message; notifications get none."""
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result: dict[str, Any] = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "jarvis", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(str(params.get("name")), params.get("arguments") or {}, jarvis)
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Unknown method {method}."}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve() -> None:
    stdin = open(sys.stdin.fileno(), encoding="utf-8", closefd=False)  # noqa: SIM115 - Windows' default would break Hindi
    for line in stdin:
        if not line.strip():
            continue
        try:
            out = handle(json.loads(line))
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "That wasn't JSON."}}
        if out is not None:
            sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    serve()
