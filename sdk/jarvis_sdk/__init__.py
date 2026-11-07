"""Use a running JARVIS from Python.

    from jarvis_sdk import Jarvis

    jarvis = Jarvis()
    print(jarvis.ask("What's on my calendar tomorrow?"))

JARVIS has to be running. On the same PC it finds itself; elsewhere (Docker) pass url and token, or set JARVIS_URL and
JARVIS_TOKEN. Requests go through the same permissions as the chat box, and anything that needs a yes waits in the app."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

__version__ = "1.0.0"
TIMEOUT_S = 900.0


class JarvisError(Exception):
    pass


def _runtime() -> dict[str, Any]:
    base = Path(os.environ.get("JARVIS_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JARVIS")
    try:
        return json.loads((base / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise JarvisError("JARVIS isn't running. Start it first.") from e


class Jarvis:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = TIMEOUT_S) -> None:
        url, token = url or os.environ.get("JARVIS_URL"), token or os.environ.get("JARVIS_TOKEN")
        if not url or not token:
            found = _runtime()
            url, token = url or f"http://127.0.0.1:{found['port']}", token or found["token"]
        self.url, self._token, self.timeout = url.rstrip("/"), token, timeout

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - the address is JARVIS's own
                return json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8"))
                message = detail.get("detail") or detail.get("message") or str(e)
            except ValueError:
                message = str(e)
            raise JarvisError(str(message)) from e
        except urllib.error.URLError as e:
            raise JarvisError(f"Couldn't reach JARVIS: {e.reason}") from e

    def ask_full(self, message: str, conversation_id: str | None = None, outside: bool = False) -> dict[str, Any]:
        """One request: {status, result, conversation_id}. outside: it came from another AI, so every action needs a yes."""
        return self._call("POST", "/api/ask", {"message": message, "conversation_id": conversation_id, "outside": outside})

    def ask(self, message: str, conversation_id: str | None = None) -> str:
        """Ask JARVIS something, or to do something, and get its answer."""
        out = self.ask_full(message, conversation_id)
        if out.get("status") == "failed":
            raise JarvisError(str(out.get("result")))
        return str(out.get("result") or "")

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """One search across memories, conversations, the calendar, stored documents and connected apps."""
        return self._call("GET", f"/api/search?q={urllib.parse.quote(query)}&limit={limit}")

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health")

    def apps(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/apps")

    def agents(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/agents")

    def create_agent(self, name: str, goal: str, every_min: int = 60) -> dict[str, Any]:
        return self._call("POST", "/api/agents", {"name": name, "goal": goal, "every_min": every_min})

    def wake_agent(self, agent_id: str) -> dict[str, Any]:
        return self._call("POST", f"/api/agents/{urllib.parse.quote(agent_id, safe='')}/wake", {})

    def workflows(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/workflows")

    def run_workflow(self, name: str, input: str = "") -> dict[str, Any]:  # noqa: A002
        found = next((w for w in self.workflows() if name in (w["id"], w["name"])), None)
        if not found:
            raise JarvisError(f"There's no workflow called {name}.")
        return self._call("POST", f"/api/workflows/{found['id']}/run", {"input": input})
