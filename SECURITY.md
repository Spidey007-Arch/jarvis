# Security policy

JARVIS can run commands, change files and act in other apps on the PC it runs on, so security problems matter a lot
here. Thank you for looking.

## Reporting a vulnerability

Please report it **privately**, not in a public issue:

- [**Report a vulnerability**](https://github.com/Spidey007-Arch/jarvis/security/advisories/new) (the Security tab →
  Report a vulnerability). Only the maintainers see it.

Please include what an attacker can do, the steps or a proof of concept, and the version (commit) you tested. We'll
acknowledge the report, keep you updated, and credit you in the fix unless you'd rather not be named.

## What counts

Especially:
- A web page, email, document, message or connector result that makes JARVIS act without the user's approval
  (prompt injection that gets past the permission checks)
- Reading or sending out saved keys, the backend token, or private data
- Getting past the sandbox, the "paying stays with the user" rule, or the permission locks
- Anything that lets another program, user or device control JARVIS's backend or the phone link

Out of scope: problems that need an attacker who already runs code as the same Windows user (they can read that
user's data directly), and denial of service against your own PC.

## Supported versions

Only the latest commit on `main` is supported while JARVIS is in early development.
