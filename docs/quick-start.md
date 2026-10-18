# Quick start

The short path from a clean checkout to a first answer. For depth, see the [setup guide](../setup.md): [Prerequisites](../setup.md#1-prerequisites), [Setup](../setup.md#setup), [Running](../setup.md#5-run-the-app) and [Testing](../README.md#testing).

## What you need

- Linux, Python 3.14+ and [uv](https://docs.astral.sh/uv/) — `bootstrap.sh` installs uv if it is missing.
- Docker, or a native SereneDB — `bootstrap.sh` handles `--db docker`, `--db native` and `--db existing`.
- Model API keys in `.env`, built from `.env.example`: `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY` and `TYPESAFE_API_KEY`.

## Steps

1. **One-command setup.**

   ```bash
   bash ./bootstrap.sh
   ```

   That is the whole setup: it installs dependencies, creates `.env`, starts SereneDB, creates the embedding secret, and loads the ownership table and the corpora. It skips each step that is already done, so re-running it is safe. Add `--samples` to load loghub's fast 2,000-line corpora instead of the full ones.

2. **Start the API.**

   ```bash
   bash ./backend.sh
   ```

   Starts the FastAPI backend and provisions a demo login account.

3. **Start the UI.**

   ```bash
   bash ./frontend.sh
   ```

   Runs the Streamlit app (default port 8501).

4. **Ask a question.** Open the UI, sign in, pick a role, and ask about the logs. Answers show their findings ledger and cited query results.

5. **Optional — the MCP server.** `bash ./mcp_server.sh` starts the MCP service so Claude can use the same tools. See [mcp-server.md](mcp-server.md).

## Verify

The offline test suite needs no external services:

```bash
uv run pytest
```

The gated live suites (real corpora, real classifier calls) are skipped by default; run them alone, as described in the README's [Testing](../README.md#testing) section.
