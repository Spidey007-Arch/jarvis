"""Prompts and JSON schemas for memory extraction and consolidation."""
from __future__ import annotations

import json
from typing import Any

_TYPES = ["identity", "preference", "goal", "project", "relationship", "event", "behavior"]

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["create", "update", "delete"]},
                    "target_id": {"type": "string", "description": "id of the existing memory for update/delete; empty for create"},
                    "type": {"type": "string", "enum": _TYPES},
                    "subject": {"type": "string"},
                    "content": {"type": "string"},
                    "importance": {"type": "number"},
                },
                "required": ["op", "type", "subject", "content", "importance"],
            },
        }
    },
    "required": ["operations"],
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"content": {"type": "string"}, "importance": {"type": "number"}},
                "required": ["content", "importance"],
            },
        }
    },
    "required": ["memories"],
}

_EXTRACT_RULES = """You maintain the long-term memory of JARVIS, a personal assistant, about its one user.
Read the latest exchange and decide which durable facts about the USER should be saved, updated or removed.

Memory types:
- identity: name, how they want to be addressed, language, location, occupation, basic profile
- preference: likes, dislikes, tastes, tools and ways of working they prefer
- goal: what they are working toward
- project: an ongoing project and its current state
- relationship: a person in their life and that person's role (subject like "person: <name>")
- event: a significant happening tied to context (a win, a setback, a deadline met)
- behavior: how they like JARVIS to interact (short answers, technical depth, tone)

Rules:
- SAVE only concrete, durable facts stated or clearly implied by the user. One fact per memory.
- UPDATE (op "update" with target_id) when a fact replaces an existing memory listed below, e.g. a changed
  preference or a project that moved to a new stage. Write the full new fact, not a diff.
- DELETE (op "delete" with target_id) when the user says something is no longer true or asks to forget it.
- A CORRECTION ("no, it's X", "actually...", "not anymore", "I meant...", or JARVIS getting a fact wrong and the
  user fixing it) is always an UPDATE of the memory it corrects, never a new memory next to the old one.
- IGNORE greetings, small talk, filler, questions to JARVIS, one-off requests, and anything about JARVIS itself.
- Never store secrets: passwords, API keys, card or account numbers, government IDs.
- subject: a short, stable lowercase key so related facts group together, e.g. "name", "preferred address",
  "favorite music", "project: jarvis", "person: priya". Reuse the subject of an existing memory when it matches.
- content: one clear sentence written in the third person about the user, e.g. "Prefers short answers in the morning."
- importance: 0.0-1.0. Name and address 0.9+, core goals and active projects 0.7-0.8, minor preferences 0.3-0.5.
- If nothing is worth remembering, return an empty operations list. That is the common case."""


def extract_prompt(existing: list[dict[str, Any]], user_text: str, assistant_text: str) -> str:
    known = [
        {"id": m["id"], "type": m["type"], "subject": m["subject"], "content": m["content"]} for m in existing
    ]
    return (
        f"{_EXTRACT_RULES}\n\nExisting memories that may be related (use their ids for update/delete):\n"
        f"{json.dumps(known, ensure_ascii=False)}\n\n"
        f"Latest exchange:\nUSER: {user_text}\nJARVIS: {assistant_text}\n"
    )


def summary_prompt(type_: str, subject: str, rows: list[dict[str, Any]]) -> str:
    items = [{"content": r["content"], "importance": r["importance"]} for r in rows]
    return (
        "These memories about the user share one subject and have accumulated over time. Merge them into the "
        "smallest set of clear, non-overlapping facts. Where facts conflict, the later item (listed last) wins. "
        "Keep every distinct, still-true detail; drop duplicates and outdated statements. "
        "Write each fact as one third-person sentence and keep the highest relevant importance.\n\n"
        f"type: {type_}\nsubject: {subject}\nmemories (oldest first):\n{json.dumps(items, ensure_ascii=False)}"
    )
