## What this changes

<!-- What and why, in a few sentences. Link the issue: "Closes #12". -->

## How I checked it

- [ ] Backend tests pass (`cd backend; ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`)
- [ ] Agent tests pass (`cd agent; ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`)
- [ ] `npx tsc --noEmit` is clean
- [ ] Tried it in the app: <!-- what you did and what happened -->

## Safety

- [ ] New actions go through `permissions.py`, and outside text stays data, not instructions
- [ ] No keys, tokens, logs or personal data are committed
