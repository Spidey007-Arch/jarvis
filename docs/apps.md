# Apps and search

JARVIS can read these apps directly. It only reads: nothing here can send, change or delete anything in the app. Set
them up in **Customize > Apps**.

| App | What you need |
|---|---|
| RSS news feeds | Nothing. A couple of feeds are on from the start; add your own |
| Hacker News | Nothing |
| Obsidian | Nothing, if Obsidian is installed: JARVIS finds the vault it last opened |
| Email (IMAP) | Mail server, address and an app password from your provider |
| GitHub notifications | A personal access token with the notifications permission |
| Granola | An API key from the Granola app |
| Oura | An access token from Oura's developer site |
| Google Drive, Contacts and Tasks | Your own free Google app, then sign in |
| Dropbox | Your own free Dropbox app, then sign in |
| Strava | Your own free Strava app, then sign in |

Keys are stored encrypted on your PC and go only to that app.

## Why some apps need "your own app"

Google, Dropbox and Strava don't offer personal tokens, so each person registers a small free app with them. JARVIS has
no server and no account of its own, so nobody else ever holds your access.

1. **Google:** in Google Cloud Console, create a project, enable the Drive, People and Tasks APIs, and create an OAuth
   client of type *Desktop app*.
2. **Dropbox:** at dropbox.com/developers, create an app with the `files.content.read` permission and the redirect
   address `http://127.0.0.1:53682/`.
3. **Strava:** at strava.com/settings/api, create an application with the callback domain `127.0.0.1`.

Paste the ID and secret into JARVIS and press **Sign in**. The answer comes back to your own PC.

## One search

Ask "find everything about the Pune trip", or use the search box at the top of Customize > Apps. It looks through your
memories, past conversations, calendar, stored documents and every connected app. If one app fails, the others still
answer and JARVIS tells you which one didn't.
