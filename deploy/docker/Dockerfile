# JARVIS's backend on a server or NAS: chat, memory, automations, agents, workflows, research and app connections,
# used through the command line, the SDK, MCP or A2A. Controlling a PC needs the Windows desktop agent, which can't run
# here, so the tools that touch a desktop say so.
FROM python:3.12-slim

WORKDIR /app
COPY deploy/docker/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/jarvis_backend ./jarvis_backend
COPY common/jarvis_common ./jarvis_common

RUN useradd --create-home jarvis && mkdir /data && chown jarvis /data
USER jarvis

ENV JARVIS_DATA_DIR=/data JARVIS_HOST=0.0.0.0 JARVIS_PORT=8765 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["python", "-m", "jarvis_backend"]
