import os
import sys

import uvicorn

from jarvis_common import logs
from .main import create_app


def main() -> int:
    log = logs.setup("backend")
    try:
        port = int(os.environ.get("JARVIS_PORT", "8765"))
    except ValueError:
        log.error("JARVIS_PORT is not a number: %r", os.environ.get("JARVIS_PORT"))
        return 2
    # Docker sets both: the window isn't there to share a token with, and the port must reach outside the container.
    app = create_app(port, os.environ.get("JARVIS_TOKEN") or None)
    config = uvicorn.Config(app, host=os.environ.get("JARVIS_HOST", "127.0.0.1"), port=port, log_config=None, ws_ping_interval=None)
    server = uvicorn.Server(config)
    app.state.server = server
    try:
        server.run()
    except Exception:  # noqa: BLE001
        log.exception("backend crashed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
