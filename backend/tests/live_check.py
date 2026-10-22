"""Live end-to-end check against a running backend with a real Gemini key.

Usage: python live_check.py <port> <token>
Run it against a throwaway data directory: it writes conversations and memories.
"""
from __future__ import annotations

import json
import sys
import time

import httpx

PORT, TOKEN = int(sys.argv[1]), sys.argv[2]
BASE = f"http://127.0.0.1:{PORT}"
H = {"Authorization": f"Bearer {TOKEN}"}


def chat(message: str, conv: str | None = None) -> tuple[str, str, dict]:
    events: dict = {"notice": [], "memory": None, "error": None}
    reply, conv_id = [], conv
    t0 = time.perf_counter()
    first = None
    with httpx.stream("POST", f"{BASE}/api/chat", headers=H, json={"message": message, "conversation_id": conv}, timeout=120) as r:
        if r.status_code != 200:
            r.read()
            raise SystemExit(f"chat failed {r.status_code}: {r.text}")
        event = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                if event == "meta":
                    conv_id = d["conversation_id"]
                elif event == "delta":
                    first = first or time.perf_counter()
                    reply.append(d["text"])
                elif event == "memory":
                    events["memory"] = d
                elif event == "notice":
                    events["notice"].append(d["message"])
                elif event == "error":
                    events["error"] = d["message"]
    events["first_token_s"] = round((first or time.perf_counter()) - t0, 2)
    return "".join(reply), conv_id, events


def memories() -> list[dict]:
    return httpx.get(f"{BASE}/api/memories", headers=H).json()


def wait_for(pred, timeout=45) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        ms = memories()
        if pred(ms):
            return ms
        time.sleep(2)
    return memories()


def show(ms: list[dict]) -> None:
    for m in ms:
        print(f"      [{m['type']}] {m['subject']}: {m['content']} (imp {m['importance']})")


def step(title: str, msg: str, conv=None):
    print(f"\n=== {title}\nYOU: {msg}")
    reply, conv, ev = chat(msg, conv)
    print(f"JARVIS: {reply.strip()}")
    print(f"   first token {ev['first_token_s']}s | memory used {ev['memory']} | notices {ev['notice']} | error {ev['error']}")
    return conv


print("settings:", httpx.get(f"{BASE}/api/settings", headers=H).json())

c1 = step("1. greeting + facts", "Good evening. I'm Tony, and I'm building an AI desktop assistant called JARVIS in Python and Rust.")
ms = wait_for(lambda ms: len(ms) >= 2)
print("   memories now:"); show(ms)

step("2. preference", "For future reference, I prefer short answers.", c1)
ms = wait_for(lambda ms: any("short" in m["content"].lower() for m in ms))
print("   memories now:"); show(ms)

step("3. preference changes", "Actually, scratch that. I now prefer detailed, technical answers.", c1)
ms = wait_for(lambda ms: any("detail" in m["content"].lower() for m in ms))
print("   memories now:"); show(ms)
superseded = httpx.get(f"{BASE}/api/memories?status=superseded", headers=H).json()
print("   superseded:"); show(superseded)

step("4. recall in a NEW conversation", "Remind me, what project am I working on and how do I like my answers?")

c5 = step("5. small talk (should store nothing new)", "Ha, thanks. Nice weather today, isn't it?")
before = len(memories())
time.sleep(12)
print(f"   memory count before {before}, after {len(memories())}")

step("6. forget", "Please forget my preference about answer length.", c5)
ms = wait_for(lambda ms: not any(("detail" in m["content"].lower() or "short" in m["content"].lower()) for m in ms), 45)
print("   memories now:"); show(ms)
print("\nstats:", httpx.get(f"{BASE}/api/memories/stats", headers=H).json())
