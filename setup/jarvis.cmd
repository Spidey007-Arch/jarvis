@echo off
rem The jarvis command, with nothing to install: put this folder on your PATH, or run it from here.
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\sdk;%PYTHONPATH%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m jarvis_sdk.cli %*
