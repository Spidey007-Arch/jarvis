# Developers

All of these talk to a running JARVIS and go through the same permissions as chat. **Settings > Developers** shows
the details for your PC.

## Command line

Run `setup\jarvis.cmd`, or add the `setup` folder to your PATH:

```
jarvis ask "what's on tomorrow?"
jarvis chat
jarvis search dentist
jarvis status
jarvis apps
jarvis agents
jarvis agent "laptop price" "tell me when it drops under 60000" --every 120
jarvis wake <agent id>
jarvis workflows
jarvis run "weekly report" --input "this week"
```

## Python SDK

From the `sdk` folder, `pip install .`, then:

```python
from jarvis_sdk import Jarvis

jarvis = Jarvis()
print(jarvis.ask("What's on my calendar tomorrow?"))
print(jarvis.search("dentist")["results"])
jarvis.create_agent("Laptop price", "Tell me when it drops under 60000", every_min=120)
print(jarvis.run_workflow("weekly report", "this week")["result"])
```

It finds a JARVIS on the same PC by itself. For one elsewhere, pass `url` and `token`, or set `JARVIS_URL` and
`JARVIS_TOKEN`.

## MCP server

Any app that uses MCP servers, such as Claude Desktop, VS Code or Cursor, can use JARVIS. Copy the block from
Settings > Developers into that app. It gives the app three tools: `ask_jarvis`, `search_jarvis` and `jarvis_status`.
The app starts it itself, so it opens no port.

Because another AI is asking, every action waits for your yes in JARVIS, even with Full control on.

## Agents talking to agents (A2A)

JARVIS speaks the open [A2A protocol](https://a2a-protocol.org). It can always ask another agent, and asks you first each
time. If that agent needs a key, save it in Customize > API keys under the agent's host name.

Other agents can ask JARVIS once you turn this on in Settings > Developers. It listens only on `127.0.0.1`, uses its own
key, and turning it off cancels the key. Every action they ask for waits for your yes.

## Coding agents

JARVIS can hand a job in a project folder to [Claude Code](https://claude.com/claude-code), OpenCode or OpenHands, if
installed: "get Claude Code to fix the failing test in my project". It asks first, the tool works by itself with a time
limit, and its report comes back. Each tool signs in on its own, and JARVIS never sees its keys.
