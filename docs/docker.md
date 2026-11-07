# Docker

Docker runs JARVIS's backend on a server or NAS: chat, memory, automations, agents, workflows, research and most app
connections, used through the [command line, SDK, MCP or A2A](developers.md). Ollama comes with it, for local AI.

It can't control a PC: that needs the Windows desktop agent. Voice, the window and the screen and mouse tools don't
work here, and they say so. For those, use the normal install.

## Run it

1. Copy `deploy/docker/.env.example` to `.env`, and set `JARVIS_TOKEN` to a long random value.
2. Start it:

```
docker compose --env-file .env -f deploy/docker/docker-compose.yml up -d
```

3. With an NVIDIA card and the NVIDIA Container Toolkit, add the GPU file:

```
docker compose --env-file .env -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.gpu.yml up -d
```

4. Get a model: `docker compose -f deploy/docker/docker-compose.yml exec ollama ollama pull llama3.2`
5. Talk to it:

```python
from jarvis_sdk import Jarvis

jarvis = Jarvis(url="http://127.0.0.1:8765", token="your token")
```

It's reachable only from the same machine. To use it from elsewhere, put a proxy with HTTPS in front of it, and keep the
token safe: it is full access to your JARVIS.

There's no first-run window in Docker yet, so choosing the AI provider has to go through `PUT /api/settings`.
