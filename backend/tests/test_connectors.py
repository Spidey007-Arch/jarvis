"""Connectors: JARVIS's own MCP client talks to a real (tiny) MCP server over stdio: handshake, tool list, a call,
an error, and read-only tools needing no permission while changing ones do."""
import asyncio
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend import permissions, toolsearch  # noqa: E402
from jarvis_backend.connectors import ConnectorError, Connectors  # noqa: E402
from jarvis_backend.tools import ToolSpec  # noqa: E402

SERVER = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue  # a notification
        m, rid = msg["method"], msg["id"]
        if m == "initialize":
            res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "notes-server"}}
        elif m == "tools/list":
            res = {"tools": [
                {"name": "search_notes", "description": "Search the notes", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                 "annotations": {"readOnlyHint": True}},
                {"name": "add_note", "description": "Add a note", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
        elif m == "tools/call":
            a = msg["params"]
            if a["name"] == "add_note" and not a["arguments"].get("text"):
                res = {"content": [{"type": "text", "text": "text is required"}], "isError": True}
            else:
                res = {"content": [{"type": "text", "text": "found: buy milk" if a["name"] == "search_notes" else "added"}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
''')


class FakeSettings:
    def __init__(self, script: str) -> None:
        self.items = [{"name": "notes", "kind": "local", "command": sys.executable, "args": [script], "enabled": True}]

    def get(self, key):
        return self.items if key == "connectors" else None

    def service_key(self, name):
        return json.dumps({"env": {"NOTES_TOKEN": "x"}})


class StdioConnector(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        script = Path(self.dir.name) / "server.py"
        script.write_text(SERVER)
        self.c = Connectors(FakeSettings(str(script)))
        await self.c.ensure()

    async def asyncTearDown(self) -> None:
        await self.c.close()
        await asyncio.sleep(0.2)
        self.dir.cleanup()

    async def test_tools_listed_called_and_errors_reported(self) -> None:
        self.assertEqual(self.c.status["notes"]["state"], "connected")
        self.assertEqual(sorted(self.c.tools), ["notes__add_note", "notes__search_notes"])
        out = await self.c.call("notes__search_notes", {"q": "milk"})
        self.assertEqual(out["result"], "found: buy milk")
        with self.assertRaises(ConnectorError):
            await self.c.call("notes__add_note", {})

    async def test_reading_needs_no_permission_changing_does(self) -> None:
        read = ToolSpec("notes__search_notes", "", {}, risk="connector_read")
        write = ToolSpec("notes__add_note", "", {}, risk="connector")
        self.assertEqual(permissions.decide(read, {}), ("allow", None))
        self.assertEqual(permissions.decide(write, {}), ("ask", "connectors"))

    async def test_tool_search_finds_connector_tools_by_name(self) -> None:
        found = [t["name"] for t in toolsearch.find("search my notes for milk", extra=self.c.schemas())]
        self.assertIn("notes__search_notes", found)


if __name__ == "__main__":
    unittest.main()
