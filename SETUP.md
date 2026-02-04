# Plaud MCP Server Setup

Quick setup guide for the Plaud MCP server.

## 1. Get Plaud API Credentials

1. Go to https://platform.plaud.cn
2. Sign up / Log in
3. Navigate to "Developer" or "API" section
4. Create a new API application
5. Copy your `client_id` and `secret_key`

## 2. Install the Server

```bash
cd /path/to/Claude-MCPs/Plaud

# Using uv (recommended)
uv venv
source .venv/bin/activate
uv pip install -e .

# Or using pip
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:
```env
PLAUD_CLIENT_ID=your_client_id
PLAUD_SECRET_KEY=your_secret_key
```

## 4. Test the Server

```bash
# Run in stdio mode
plaud-mcp

# Or in HTTP mode for testing
plaud-mcp --http
```

## 5. Add to Claude Desktop

Edit `~/.claude.json`:

```json
{
  "mcpServers": {
    "plaud": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/davidlin/Claude Project/Claude-MCPs/Plaud",
        "run",
        "plaud-mcp"
      ]
    }
  }
}
```

**Restart Claude Desktop** to load the server.

## 6. Try It Out

In Claude:
```
Use get_recent_files to show me my recent Plaud recordings
```

## Troubleshooting

### "Module not found"
Make sure you installed with `-e .`:
```bash
uv pip install -e .
```

### Authentication Fails
- Check credentials in `.env`
- Verify API application is active at https://platform.plaud.cn

### Server Not Appearing
- Restart Claude Desktop
- Check paths in `~/.claude.json`
- Verify `.env` file exists in the Plaud directory

## That's It!

No database setup. No migrations. Just credentials and go.

See `README.md` for full documentation.
