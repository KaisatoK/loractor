# Screenshots

This folder holds the UI screenshots referenced from [docs/index.md](../index.md). Until real captures are added, the landing page uses [`screenshot-placeholder.svg`](screenshot-placeholder.svg).

## What to capture

- `ui-chat.png` — the main chat with a role-specific answer: the findings ledger and the cited tables.
- `ui-progress.png` — the live progress panel mid-investigation.
- `ui-roles.png` — the role-switch buttons under an answer.
- `mcp-claude.png` _(optional)_ — Claude connected to the MCP server.

## How

1. Start the backend and the UI:

   ```bash
   bash ./backend.sh
   bash ./frontend.sh
   ```

2. Sign in and ask a question (or open a worked investigation).
3. Capture at a reasonable width (1400px or more) so the answer and its tables are legible.

## Where

Save the PNG files into this folder (`docs/assets/`), then replace the placeholder reference in [`docs/index.md`](../index.md). The landing page currently has one line:

```markdown
![The loractor UI](assets/screenshot-placeholder.svg)
```

Replace the `assets/screenshot-placeholder.svg` reference with the real file (for example `assets/ui-chat.png`), and add more image lines for the other captures.

## Safety

Never capture API keys, tokens, or personal data in a screenshot. Keep the actual secrets out of committed files.
