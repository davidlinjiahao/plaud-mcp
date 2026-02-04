# Plaud MCP Server

A lightweight Model Context Protocol (MCP) server that provides direct access to your Plaud transcripts.

## Architecture

This is a **thin wrapper** MCP server following best practices from `modelcontextprotocol/python-sdk`:

```
Claude Agent → MCP Server → Plaud API → Return data
```

No database. No caching. No sync service. Just direct API calls using your Plaud Desktop authentication.

## Features

- **Zero Configuration**: Uses authentication from Plaud Desktop app (no API keys needed)
- **Direct API Access**: Real-time queries to Plaud API (no stale data)
- **FastMCP**: Built with the official Python MCP SDK
- **Search**: Client-side transcript search
- **Summaries**: Access AI-generated summaries

## Prerequisites

1. **Plaud Desktop App**
   - Install Plaud Desktop from https://plaud.ai
   - Sign in to your Plaud account
   - The MCP extracts authentication from the desktop app automatically

2. **Python 3.10+**
   ```bash
   python --version  # Should be 3.10 or higher
   ```

3. **uv** (recommended) or pip
   ```bash
   # Install uv if you don't have it
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

## Installation

```bash
cd Plaud

# Create virtual environment and install
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e .
```

Or with pip:
```bash
pip install -e .
```

## Configuration

No configuration required! The MCP automatically uses your Plaud Desktop authentication.

Optional: Create `.env` file for logging configuration:
```env
PLAUD_LOG_LEVEL=INFO
```

## Usage

### Adding to Claude Desktop

Add to your Claude Desktop MCP configuration (`~/.claude.json`):

```json
{
  "mcpServers": {
    "plaud": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/Claude-MCPs/Plaud",
        "run",
        "plaud-mcp"
      ]
    }
  }
}
```

Or if using pip-installed version:
```json
{
  "mcpServers": {
    "plaud": {
      "command": "/path/to/Plaud/.venv/bin/plaud-mcp"
    }
  }
}
```

Restart Claude Desktop to load the MCP server.

### Running Standalone

```bash
# Stdio mode (for Claude Desktop)
plaud-mcp

# HTTP mode (for development)
plaud-mcp --http
```

## MCP Tools

### `check_connection`
Check if Plaud Desktop is available and authenticated.

**Output:**
```json
{
  "status": "connected",
  "total_files": 36,
  "message": "Successfully connected to Plaud via Desktop app"
}
```

---

### `get_file_count`
Get total number of Plaud files.

**Output:**
```json
{
  "total": 36
}
```

---

### `get_recent_files`
Get files from the last N days.

**Input:**
```json
{
  "days": 7
}
```

**Output:** List of file objects with id, filename, date, duration, has_transcript, has_summary

---

### `get_files`
Get files with optional date filters.

**Input:**
```json
{
  "start_date": "2024-01-01",
  "end_date": "2024-01-31",
  "limit": 100
}
```

**Output:** List of file objects

---

### `get_file`
Get metadata for a specific file.

**Input:**
```json
{
  "file_id": "file-id-here"
}
```

**Output:** File metadata object

---

### `get_transcript`
Get full transcript for a file.

**Input:**
```json
{
  "file_id": "file-id-here"
}
```

**Output:**
```json
{
  "file_id": "...",
  "transcript": "Full transcript text with speaker labels...",
  "segment_count": 122,
  "segments": [...]
}
```

---

### `get_summary`
Get AI-generated summary for a file.

**Input:**
```json
{
  "file_id": "file-id-here"
}
```

**Output:**
```json
{
  "file_id": "...",
  "content": "Markdown summary content...",
  "header": {"headline": "Meeting Title"},
  "category": "meeting"
}
```

---

### `search_transcripts`
Search through recent transcripts.

**Input:**
```json
{
  "query": "meeting notes",
  "days": 30
}
```

**Output:** List of matching files with excerpts

**Note:** Searches client-side. May take a few seconds for large result sets.

---

## Common Use Cases

### Daily Review
```
Use get_recent_files with days 1 to show me today's recordings
```

### Get Full Transcript
```
Use get_transcript with file_id "abc123" to get the full transcript
```

### Search for Topic
```
Use search_transcripts to find all recordings about "product roadmap" from the last 30 days
```

### Check Connection
```
Use check_connection to verify Plaud Desktop is available
```

## How It Works

The MCP extracts your authentication token from Plaud Desktop's local storage:

1. When you sign in to Plaud Desktop, it stores a JWT token locally
2. The MCP reads this token from `~/Library/Application Support/Plaud/Local Storage/leveldb`
3. It uses this token to authenticate with the Plaud consumer API at `api.plaud.ai`
4. No API keys or developer credentials needed!

**Requirements:**
- Plaud Desktop must be installed
- You must be signed in to Plaud Desktop
- Token expires after ~1 year and is automatically refreshed when you use Plaud Desktop

## Development

### Setup
```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Linting
```bash
ruff check src/
ruff format src/
```

### Type Checking
```bash
pyright src/
```

### Testing
```bash
pytest tests/
```

## Why This Architecture?

### Thin Wrapper Pattern
This server follows the patterns from:
- **modelcontextprotocol/python-sdk** - Official Python SDK
- **modelcontextprotocol/servers** - Reference implementations

### Benefits
- **Simple**: ~300 lines of code
- **Zero Config**: Uses existing Plaud Desktop auth
- **Reliable**: No sync issues, no stale data
- **Real-time**: Always fetches latest from Plaud
- **Maintainable**: No database schema to manage
- **Stateless**: Can restart anytime

## Troubleshooting

### "Plaud Desktop not found"

- Ensure Plaud Desktop is installed
- Sign in to Plaud Desktop
- The app stores auth in `~/Library/Application Support/Plaud/`

### MCP Server Not Appearing

- Restart Claude Desktop after modifying `~/.claude.json`
- Check paths in the configuration are correct
- Verify Python environment is activated

### Search is Slow

The `search_transcripts` tool fetches files and searches client-side:
- Reduce the `days` parameter
- Use `get_files` with date filters first

## License

MIT

## Resources

- [Plaud](https://plaud.ai)
- [MCP Protocol](https://modelcontextprotocol.io)
- [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
