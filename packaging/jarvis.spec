# PyInstaller spec: builds the two Python parts of JARVIS as folders with their own .exe, so the installed app
# needs no Python. Run through setup/build-release.ps1, which puts the result in .dist/ for the Tauri bundle.
# The app (src-tauri/src/supervisor.rs) starts <install dir>/jarvis-backend/jarvis-backend.exe and
# <install dir>/jarvis-agent/jarvis-agent.exe.
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
PATHS = [os.path.join(ROOT, d) for d in ("backend", "agent", "common", "sdk")]
sys.path[:0] = PATHS  # collect_submodules imports the packages while the spec runs, before pathex applies
EXCLUDES = ["google.genai.tests", "numpy.f2py.tests", "onnxruntime.quantization", "tkinter", "matplotlib", "IPython", "pytest", "PyInstaller"]


def gather(*packages, full=()):
    datas, binaries, hidden = [], [], []
    for p in packages:
        hidden += collect_submodules(p)
    for p in full:  # packages with DLLs, templates or fonts beside the code
        d, b, h = collect_all(p)
        datas += d
        binaries += b
        hidden += h
    return datas, binaries, hidden


# The agent's sandbox runs any Python the user or JARVIS writes, so it gets the whole standard library, not only the
# modules JARVIS itself imports (minus GUI, test and dev-tool packages).
_SKIP = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "unittest", "lib2to3", "ensurepip", "venv", "pydoc_data",
         "distutils", "msilib", "this", "antigravity", "__phello__", "_tkinter", "xxsubtype", "xxlimited", "xxlimited_35"}
STDLIB = []
for _m in sorted(sys.stdlib_module_names - _SKIP):
    try:
        __import__(_m)
    except Exception:  # noqa: BLE001 - POSIX-only modules and the like
        continue
    STDLIB += [_m] + (collect_submodules(_m, filter=lambda n: ".test" not in n) if hasattr(sys.modules[_m], "__path__") else [])


def build(name, script, packages, full, extra_datas=(), hidden_extra=()):
    datas, binaries, hidden = gather(*packages, full=full)
    hidden += list(hidden_extra)
    a = Analysis([os.path.join(SPECPATH, script)], pathex=PATHS, binaries=binaries, datas=datas + list(extra_datas),
                 hiddenimports=hidden, excludes=EXCLUDES, noarchive=False)
    pyz = PYZ(a.pure)
    # console=True: the app hides the console itself (CREATE_NO_WINDOW) and reads the logs; a windowed .exe has no stdout.
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=True, upx=False,
              icon=os.path.join(ROOT, "src-tauri", "icons", "icon.ico"))
    return COLLECT(exe, a.binaries, a.datas, name=name, upx=False)


build("jarvis-backend", "launch_backend.py",
      packages=["jarvis_backend", "jarvis_common", "jarvis_sdk", "uvicorn", "google.genai", "fastapi", "pydantic", "httpx", "websockets"],
      full=["tzdata", "cryptography", "pypdf"],  # tzdata: Windows has no time-zone database of its own
      extra_datas=[(os.path.join(ROOT, "backend", "jarvis_backend", "data"), "jarvis_backend/data"),
                   (os.path.join(ROOT, "community"), "community"),
                   (os.path.join(ROOT, "sdk"), "sdk")])

build("jarvis-agent", "launch_agent.py",
      packages=["jarvis_agent", "jarvis_common", "websockets"],
      hidden_extra=STDLIB,
      full=["tzdata", "sherpa_onnx", "onnxruntime", "sounddevice", "docx", "pptx", "openpyxl", "fpdf", "pypdf", "PIL", "numpy", "pyperclip"])
