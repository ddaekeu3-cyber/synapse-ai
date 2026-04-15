---
layout: default
title: "MCP and Tool Failure Errors in AI Agents — Diagnosis and Fixes"
description: "Fix MCP tool failures, function call errors, plugin crashes, and tool schema mismatches in OpenClaw and AI agents. Step-by-step troubleshooting guide."
permalink: /guide/tool-failure-errors
---

# MCP and Tool Failure Error Guide

Tool failures are among the hardest agent errors to debug because the failure mode isn't always obvious — the agent may silently fall back to no-tool behavior, produce incorrect results, or loop trying to re-invoke a broken tool.

---

## How Tool Failures Manifest

| Failure Type | What the Agent Does |
|-------------|---------------------|
| Schema mismatch | Tool call rejected, agent tries to rephrase and retry → loop |
| Permission denied | Tool returns error, agent tries workaround → token waste |
| Timeout | Tool hangs, agent waits indefinitely → stuck |
| Plugin crash | Tool returns null/undefined, agent misinterprets → wrong output |
| Network error | Tool unreachable, agent retries → rate limit cascade |

---

## Fix 1: Validate Tool Schema Before Deployment

Most tool failures are schema mismatches — the agent passes parameters that the tool doesn't accept.

```bash
# Validate MCP tool schema
openclaw mcp validate --tool your-tool-name

# Check what the tool expects
openclaw mcp describe --tool your-tool-name --show-schema
```

Common schema errors:
- Passing `string` where tool expects `number`
- Missing required parameters
- Extra parameters the tool doesn't accept
- Nested object structure incorrect

---

## Fix 2: Set Tool Timeouts

Tools without timeouts will hang indefinitely when the upstream API is slow:

```yaml
# openclaw.config.yaml
tools:
  default_timeout_ms: 10000    # 10s timeout for all tools
  per_tool:
    web_search: 15000          # Search tools need more time
    code_executor: 30000       # Code execution can be slow
    database_query: 5000       # DB queries should be fast
```

On timeout, the tool returns an error the agent can handle — instead of blocking forever.

---

## Fix 3: MCP Server Connection Errors

**Symptom:** `MCP server disconnected` or `failed to connect to MCP server` on startup.

**Root cause:** MCP server process not running, wrong socket path, or version mismatch.

**Diagnosis:**
```bash
# Check if MCP server is running
ps aux | grep mcp

# Check socket/port
openclaw mcp list --show-status

# Check MCP server logs
openclaw mcp logs --server your-server-name --last 50
```

**Fix:**
```bash
# Restart MCP server
openclaw mcp restart --server your-server-name

# If version mismatch:
npm update @modelcontextprotocol/server-your-tool
openclaw mcp restart --server your-server-name
```

---

## Fix 4: Tool Permission Errors

**Symptom:** Tool call returns `403 Forbidden` or `permission denied`.

**Root cause:** The tool requires specific scopes or credentials that the current session doesn't have.

**Fix:**
```yaml
tools:
  your-tool-name:
    credentials:
      api_key: ${YOUR_TOOL_API_KEY}
      scopes:
        - read:data
        - write:data
```

For OAuth-based tools, re-authorize with the required scopes:
```bash
openclaw mcp authorize --server your-server-name --scope read:data,write:data
```

---

## Fix 5: Function Call Parsing Errors

**Symptom:** Agent's function call is syntactically correct but the tool returns `invalid_request`.

**Root cause:** The model generates valid JSON but with semantic errors — correct field names but wrong value types or ranges.

**Fix:** Add tool-specific validation before execution:

```python
def validate_tool_call(tool_name, params):
    validators = {
        'search': lambda p: isinstance(p.get('query'), str) and len(p['query']) > 0,
        'create_file': lambda p: p.get('path', '').startswith('/workspace/'),
    }
    validator = validators.get(tool_name)
    if validator and not validator(params):
        raise ValueError(f"Invalid params for {tool_name}: {params}")
```

---

## Fix 6: Tool Output Parsing Failures

**Symptom:** Tool call succeeds (200 OK) but agent misinterprets the output, leading to wrong actions.

**Root cause:** Tool output format changed upstream; agent's expectation is stale.

**Fix:** Add output validation:
```yaml
tools:
  your-tool-name:
    output_validation:
      required_fields: [id, status, result]
      on_missing_fields: log_and_retry
```

---

## Common MCP Error Patterns

### `spawn ENOENT` on MCP startup

The MCP server executable isn't found in PATH.

```bash
# Find where the server is installed
which mcp-server-filesystem  # or your server name
npm list -g | grep mcp

# Add to PATH or use absolute path in config
# openclaw.config.yaml:
mcp_servers:
  filesystem:
    command: /usr/local/bin/mcp-server-filesystem
```

---

### MCP Tool Returns Empty Result

Tool call returns 200 with empty `{result: null}` or `{}`.

**Causes:**
1. Tool found nothing matching the query (legitimate empty)
2. Tool silently failed and returned empty instead of error
3. Pagination: result is on page 2, tool defaults to page 1

**Fix:**
```yaml
tools:
  your-tool:
    on_empty_result:
      action: log_warning  # Don't treat empty as success
      retry: false         # Don't retry — empty might be correct
```

---

### Tool Calls in Loop After Initial Failure

Agent tries the same tool call repeatedly with minor variations.

**Root cause:** Circuit breaker not configured for tools. Agent doesn't know when to give up.

**Fix:**
```yaml
agent:
  tool_circuit_breaker:
    failure_threshold: 3
    on_open: skip_tool_and_report
    reset_timeout_ms: 60000
```

After 3 failures on the same tool, stop trying and report what was attempted.

---

## Tool Failure Checklist

Before deploying an agent that uses external tools:

- [ ] Tool schema validated with `openclaw mcp validate`
- [ ] Timeouts set per tool (not just global default)
- [ ] Circuit breaker configured for tool calls
- [ ] Tool credential scopes verified
- [ ] Empty result handling defined
- [ ] Output validation enabled for critical tools
- [ ] MCP server version pinned (don't use `latest`)

---

[← View all tool failure solutions](/synapse-ai/solutions/tool-failure/)

**Related guides:**
- [Rate Limit Errors](/synapse-ai/guide/rate-limit-errors) — when tool calls hit API limits
- [Auth Errors](/synapse-ai/guide/auth-errors) — when tool credentials fail
- [Loop / Stuck Errors](/synapse-ai/guide/loop-stuck-errors) — when tool failures cause retry storms

<div class="cta-box">
  <h3>Auto-diagnose tool failures</h3>
  <p>SynapseAI detects MCP and tool call error patterns and suggests fixes before your agent gets stuck in a retry loop.</p>
  <code>clawhub install synapse-ai</code>
</div>
