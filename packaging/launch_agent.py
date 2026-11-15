"""PyInstaller entry point for jarvis-agent.exe (a package's __main__.py cannot be the entry: relative imports)."""
import multiprocessing
import sys

multiprocessing.freeze_support()
from jarvis_agent.__main__ import main  # noqa: E402  (also runs scripts when used as python.exe: the sandbox)

sys.exit(main())
