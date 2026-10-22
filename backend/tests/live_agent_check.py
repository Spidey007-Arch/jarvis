"""Live end-to-end check of desktop actions: real model, real agent, real effects on this PC.

Usage: python live_agent_check.py <port> <token> [approve|decline]
Confirmation prompts are answered automatically (approve by default) so the run is unattended.
"""
from __future__ import annotations

import json
import sys
import threading
import time

import httpx

PORT, TOKEN = int(sys.argv[1]), sys.argv[2]
BASE = f"http://127.0.0.1:{PORT}"
H = {"Authorization": f"Bearer {TOKEN}"}


def chat(message: str, conv: str | None = None, approve: bool = True) -> tuple[str, str | None]:
    t0 = time.time()
    reply, tools, notices, error = [], [], [], None
    conv_id = conv
    with httpx.stream("POST", f"{BASE}/api/chat", headers=H, json={"message": message, "conversation_id": conv}, timeout=300) as r:
        event = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                if event == "meta":
                    conv_id = d["conversation_id"]
                elif event == "delta":
                    reply.append(d["text"])
                elif event == "tool":
                    if d["status"] == "awaiting_confirmation":
                        threading.Thread(target=lambda i=d["id"]: httpx.post(f"{BASE}/api/confirm/{i}", headers=H, json={"approve": approve})).start()
                    if d["status"] in ("done", "failed", "declined", "awaiting_confirmation"):
                        tools.append(f"{d['label']} -> {d['status']}{(': ' + d['error']) if d.get('error') else ''}")
                elif event == "notice":
                    notices.append(d["message"])
                elif event == "error":
                    error = d["message"]
    print(f"\nYOU: {message}")
    for t in tools:
        print(f"   [action] {t}")
    for n in notices:
        print(f"   [notice] {n}")
    if error:
        print(f"   [ERROR] {error}")
    print(f"JARVIS ({time.time() - t0:.0f}s): {''.join(reply).strip()}")
    return "".join(reply), conv_id


mode = "approve"
custom = sys.argv[3:]
steps = [(m, True) for m in custom] or [
    ("How is the system doing right now?", True),
    ("What's my current volume? Set it to 30 percent.", True),
    ("Put the volume back to 100.", True),
    ("Open Chrome and search for Tony Stark arc reactor.", True),
    ("Create a folder called JARVIS-live-test in my Documents, then delete it.", True),
    ("Remind me to stretch in 3 minutes.", True),
    ("What is on my screen right now? One short paragraph.", True),
    ("Use the web to find out what the latest stable version of the Rust programming language is.", True),
    ("Shut down my PC.", False),
]
conv = None
for msg, approve in steps:
    _, conv = chat(msg, conv, approve if mode == "approve" else False)
    time.sleep(8)
