---
layout: solution
title: "MCP Server Fails to Start — spawn ENOENT Error on Startup"
category: tool-failure
description: "MCP server process fails to launch with 'spawn ENOENT' error. Agent starts without the tool available. Usually caused by the server executable not being in PATH."
tags: [mcp, spawn, enoent, path, startup]
---

# MCP Server Fails to Start — spawn ENOENT Error on Startup

## Symptom
- MCP server shows `spawn ENOENT` error on agent startup
- Agent starts but the tool/server is unavailable
- No error if the server command is run manually in the same shell
- Error appears in agent logs, not in shell output

## Root Cause
`ENOENT` (No such file or directory) on spawn means the executable path doesn't exist in the environment the agent runs in. The agent process has a different `PATH` than your interactive shell — especially when run via systemd, Docker, or `npm start`.

## Fix

### Option 1: Use absolute path in MCP config

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "/usr/local/bin/mcp-server-filesystem",
      "args": ["--root", "/workspace"]
    }
  }
}
```

Find the absolute path:
```bash
which mcp-server-filesystem
# → /usr/local/bin/mcp-server-filesystem
```

### Option 2: Use npx to resolve path automatically

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
    }
  }
}
```

`npx` handles path resolution — the server doesn't need to be in PATH.

### Option 3: Pass explicit PATH to the MCP server process

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "mcp-server-filesystem",
      "args": ["--root", "/workspace"],
      "env": {
        "PATH": "/usr/local/bin:/usr/bin:/bin"
      }
    }
  }
}
```

### Option 4: Install globally and verify

```bash
npm install -g @modelcontextprotocol/server-filesystem

# Verify it's accessible
mcp-server-filesystem --help

# If still not found after global install, check npm prefix
npm config get prefix
# Add <prefix>/bin to PATH in your shell profile
```

### Diagnosis

```bash
# Check if the server is installed
npm list -g | grep mcp

# Check all candidate locations
find /usr -name "mcp-server-*" 2>/dev/null
find ~/.npm -name "mcp-server-*" 2>/dev/null

# Test spawn manually (same as agent would)
node -e "require('child_process').spawn('mcp-server-filesystem', [], {stdio: 'inherit'})"
```

## Expected Token Savings
Debugging missing MCP server tool at runtime: ~4,000 tokens
This fix: ~100 tokens (one config line change)

## Environment
- OpenClaw with MCP server configuration
- Any Node.js-based MCP server
- Affects startup when PATH differs between shell and agent process
- Source: direct experience, common MCP setup issue
