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
    app = create_app(port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, ws_ping_interval=None)
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
