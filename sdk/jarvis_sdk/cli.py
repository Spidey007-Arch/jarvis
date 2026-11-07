"""The jarvis command: talk to a running JARVIS from a terminal.

    jarvis ask "what's on tomorrow?"
    jarvis chat
    jarvis search dentist
    jarvis status | apps | agents | workflows
    jarvis agent "laptop price" "tell me when it drops under 60000" --every 120
    jarvis wake <agent id>
    jarvis run "weekly report" --input "this week"
    jarvis mcp"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import Jarvis, JarvisError, __version__


def _rows(items: list[dict[str, Any]], *keys: str) -> None:
    for x in items:
        print("  ".join(str(x.get(k, "")) for k in keys))


def _chat(j: Jarvis) -> None:
    print("Talking to JARVIS. Press Enter on an empty line to leave.")
    conv = None
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not text:
            return
        out = j.ask_full(text, conv)
        conv = out.get("conversation_id") or conv
        print(f"jarvis> {out.get('result', '')}")


def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jarvis", description="Talk to a running JARVIS.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("ask", help="ask once").add_argument("message", nargs="+")
    sub.add_parser("chat", help="keep talking")
    s = sub.add_parser("search", help="search everything JARVIS can read")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--json", action="store_true")
    for name, what in (("status", "is it running?"), ("apps", "connected apps"), ("agents", "always-running agents"), ("workflows", "saved workflows"),
                       ("mcp", "run as an MCP server, for other apps")):
        sub.add_parser(name, help=what)
    a = sub.add_parser("agent", help="start an always-running agent")
    a.add_argument("name")
    a.add_argument("goal")
    a.add_argument("--every", type=int, default=60, help="minutes between wakes")
    sub.add_parser("wake", help="wake an agent now").add_argument("id")
    r = sub.add_parser("run", help="run a workflow")
    r.add_argument("name")
    r.add_argument("--input", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build().parse_args(argv)
    try:
        if args.command == "mcp":
            from .mcp_server import serve

            serve()
            return 0
        j = Jarvis()
        if args.command == "ask":
            print(j.ask(" ".join(args.message)))
        elif args.command == "chat":
            _chat(j)
        elif args.command == "search":
            found = j.search(" ".join(args.query), args.limit)
            if args.json:
                print(json.dumps(found, indent=2, ensure_ascii=False))
            else:
                _rows(found["results"], "app", "title", "when", "url")
                for name, why in found["problems"].items():
                    print(f"({name}: {why})", file=sys.stderr)
        elif args.command == "status":
            h = j.health()
            print(f"JARVIS {h['version']} is running. Desktop agent: {'connected' if h['agent_connected'] else 'not connected'}.")
        elif args.command == "apps":
            for x in j.apps():
                print(f"{'on ' if x['connected'] else 'off'}  {x['id']:<11} {x['label']}")
        elif args.command == "agents":
            _rows(j.agents(), "id", "name", "every_min", "last_status")
        elif args.command == "workflows":
            _rows(j.workflows(), "id", "name", "last_status")
        elif args.command == "agent":
            print(f"Started {j.create_agent(args.name, args.goal, args.every)['name']}.")
        elif args.command == "wake":
            print(j.wake_agent(args.id))
        elif args.command == "run":
            print(j.run_workflow(args.name, args.input).get("result", ""))
    except JarvisError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
