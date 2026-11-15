"""PyInstaller entry point for jarvis-backend.exe (a package's __main__.py cannot be the entry: relative imports)."""
import multiprocessing
import sys

multiprocessing.freeze_support()
from jarvis_backend.__main__ import main  # noqa: E402  (also runs scripts/modules when used as python.exe)

sys.exit(main())
