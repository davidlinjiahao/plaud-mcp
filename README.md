# Plaud MCP Server

MCP server for Plaud transcripts via CDP proxy through the running Plaud Desktop app.

## How It Works

```
Claude Code → MCP Server → CDP (WebSocket) → Plaud Desktop → Plaud API
```

The MCP connects to the running Plaud Desktop Electron app via Chrome DevTools Protocol:

1. Sends `SIGUSR1` to the Plaud Desktop process to enable Node.js inspector (port 9229)
2. Connects via WebSocket to the inspector
3. Executes API calls through the app's own authenticated `$fetch` function
4. Returns results back through MCP tools

No token extraction, no cookies, no API keys. Uses the app's live authenticated session directly.

## Prerequisites

1. **Plaud Desktop** - installed and signed in
2. **Python 3.10+**
3. **uv** (recommended)

## Installation

```bash
# Install as a CLI tool (recommended)
uv tool install "plaud-mcp @ git+https://github.com/davidlinjiahao/plaud-mcp"

# Or install from local source
uv tool install --force "plaud-mcp @ ."
```

## Configuration

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "plaud": {
      "command": "${HOME}/.local/bin/plaud-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

No API keys or tokens needed. Just ensure Plaud Desktop is running.

## MCP Tools

| Tool | Description |
|------|-------------|
| `check_connection` | Verify Plaud Desktop is available |
| `get_file_count` | Total number of recordings |
| `get_recent_files` | Files from the last N days |
| `get_files` | Files with optional date filters |
| `get_file` | Metadata for a specific file |
| `get_transcript` | Full transcript with speaker labels |
| `get_summary` | AI-generated summary |
| `search_transcripts` | Search transcripts by content |

## Troubleshooting

### "Plaud Desktop is not running"
Launch the Plaud Desktop app and sign in.

### "Could not enable inspector"
The SIGUSR1 signal may have failed. Ensure Plaud Desktop is the main process, not a helper.

### Search is slow
`search_transcripts` fetches and searches client-side. Reduce the `days` parameter.

## Why CDP?

Plaud's API validates auth at the Chromium network stack level - tokens extracted from LevelDB don't work with standard HTTP clients (httpx, curl, curl_cffi with Chrome impersonation all return 401). The CDP approach bypasses this entirely by executing requests through the app's own authenticated context.

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
ruff check src/ && pyright src/
```
