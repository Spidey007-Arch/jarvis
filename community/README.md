# Community plugins

Plugins listed here show up in JARVIS under **Customize → Plugins → Community catalog**. A plugin bundles
[skills](../README.md#customize) (steps JARVIS follows for a kind of request) and connectors (MCP servers), so
other people can install what you built in one click.

## Share yours

1. In JARVIS, write your skills in **Customize → Skills** and use them for a while.
2. **Customize → Plugins → SHARE MY SKILLS** saves them as `<name>.jarvis-plugin.json` in your Documents folder.
3. Edit the file: give it a clear `id`, `name` and `description`, and remove anything personal (your paths, names,
   addresses, accounts).
4. Open a pull request that adds it to `community/plugins/<id>.json` and a line for it in `community/catalog.json`.

Every plugin is reviewed before it's merged. We check that it does what it says, that its skills never ask JARVIS to
send, pay or delete anything without the user saying so, and that every connector command is a well-known package.

## The format

```json
{
  "format": "jarvis-plugin",
  "id": "morning-routine",
  "name": "Morning routine",
  "version": "1.0",
  "author": "Your name",
  "homepage": "https://…",
  "description": "One or two sentences on what it does and what it needs.",
  "skills": [
    { "name": "Start my day", "when_to": "I say good morning", "body": "1. …\n2. …" }
  ],
  "connectors": [
    { "name": "notion", "kind": "local", "command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"], "env_names": ["NOTION_TOKEN"] },
    { "name": "github", "kind": "web", "url": "https://api.githubcopilot.com/mcp/", "needs_token": true }
  ]
}
```

| Field | Rules |
|---|---|
| `id` | 2–40 lowercase letters, digits and dashes; the same as the file name |
| `skills` | up to 20; `name` up to 80 characters, `when_to` up to 300, `body` up to 6,000 |
| `connectors` | up to 5; `local` ones may only start with `npx`, `uvx`, `docker`, `node` or `python`, with no shell characters; `web` ones need an `https://` address |
| `env_names` | the environment values a local connector needs (like `NOTION_TOKEN`); the user fills them in, they never go in the file |

When someone installs a plugin, JARVIS shows everything it adds first, including each connector's full command.
Connectors are added switched off, and the user turns each one on after reading it.
