# Contributing to JARVIS

Thanks for wanting to help. Ideas, bug reports, test reports and code are all welcome.

## Not a coder? You can still help a lot

- **Ask for something.** [Feature requests](https://github.com/Spidey007-Arch/jarvis/issues/new?template=feature_request.yml)
  are how the roadmap gets decided. The most useful ones describe the real situation ("every morning I open the same
  five things…") more than the solution.
- **Report what broke**, with the steps and the log lines (see below).
- **Test your AI provider** and [say how it went](https://github.com/Spidey007-Arch/jarvis/issues/new?template=provider_report.yml).
- **Vote** on issues you care about with a 👍, so we know what matters most.

## Logs

JARVIS writes logs to `%LOCALAPPDATA%\JARVIS\logs`: `backend.log`, `agent.log`, `error.log`, `automation.log` (every
tool call) and `crash.log` (window errors). Keys are never written there, but your messages, file names and paths can
be, so read what you paste and remove anything private.

## Setting up for development

```powershell
git clone https://github.com/Spidey007-Arch/jarvis.git
cd jarvis
powershell -ExecutionPolicy Bypass -File setup\install-deps.ps1
npm install
npx tauri dev
```

`npx tauri dev` starts the window with hot reload for the UI; the shell starts the backend and agent from `backend/`
and `agent/`. After changing Python code, close JARVIS and run `npx tauri dev` again.

See [How it works](README.md#how-it-works) and [Project layout](README.md#project-layout) for where things live.

## Before you open a pull request

1. **Run the tests**, and add tests for what you change:
   ```powershell
   cd backend;  ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
   cd ..\agent; ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
   cd ..;       npx tsc --noEmit
   ```
2. **Match the code around you**: its naming, its comment style (short comments that say *why*), and its plain-English
   messages to the user.
3. **Keep the safety rules intact.** Anything that acts on the PC goes through `permissions.py`; new tools that change
   things, send data out, or run unattended need a capability there. Text from the web, files, mail or connectors is
   data, never instructions. Paying stays with the user.
4. **Never commit secrets**: no keys, tokens, `.env` files, `settings.dat` or logs. Tests use obviously fake values.
5. **Say what you checked**: in the pull request, list how you tested it (tests, and what you tried in the app).

Small, focused pull requests are much easier to review than large ones. For anything big, open an issue first so we
can agree on the approach before you spend the time.

## Adding things JARVIS can do

- **A tool**: describe it in `backend/jarvis_backend/tools.py` (clear description, the arguments, a short label),
  implement it in the backend (`api.py` `_run_tool`) or the agent (`agent/jarvis_agent/`, an `@action`), map its risk
  in `permissions.py`, and add a test.
- **A skill** doesn't need code: write it in **Customize → Skills** and share the text in an issue.
- **A connector** doesn't need code either: any MCP server works through **Customize → Connectors**.

## Security issues

Please don't open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Code of conduct

Be kind, be patient, assume good intent. Harassment or abuse of anyone isn't welcome here.
