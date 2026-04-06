# plaud-mcp

MCP server for Plaud transcripts. macOS only.

## Commands

```bash
uv pip install -e .          # Install from source
uv pip install -e ".[dev]"   # Install with dev deps
plaud-mcp                    # Run (stdio mode, for Claude Code)
plaud-mcp --http             # Run in HTTP mode
ruff check src/              # Lint
pyright src/                 # Type check
```

## Architecture

```
src/plaud_mcp/
  server.py        — FastMCP server, 8 tools (get_files, get_transcript, search_transcripts, etc.)
  plaud_client.py  — HTTP client: decrypts JWT from Plaud Desktop's encryption.json via macOS Keychain
```

Auth flow: `encryption.json` (Chromium v10 AES-128-CBC) → macOS Keychain "Plaud Safe Storage" → PBKDF2 key → decrypt → Bearer JWT → `api.plaud.ai`

## Key Gotchas

- **macOS only** — depends on `security` CLI for Keychain access
- **Plaud Desktop must have been signed in at least once** — creates encryption.json and Keychain entry
- **Token valid ~300 days** — JWT `exp` is ~10 months from issue
- **`data_file_total` in API response** = count of files in current page, not true total
- **Plaud data dir**: `~/Library/Application Support/Plaud/`
