---
layout: default
title: "AI Agent Configuration Errors — Setup, Environment, and Config Fixes"
description: "Fix AI agent configuration errors: missing env vars, YAML syntax errors, schema mismatches, provider setup, and openclaw.config.yaml troubleshooting."
permalink: /guide/config-errors
---

# AI Agent Configuration Error Guide

Configuration errors are often the most frustrating: the agent looks broken, the error message is unhelpful, and the fix is usually a one-line change that should have been obvious. This guide covers the most common config failures.

---

## Configuration Failure Patterns

| Error | Symptom | Root Cause |
|-------|---------|-----------|
| Missing env var | `Cannot read property of undefined`, agent silent | API key not set in environment |
| YAML parse error | Agent refuses to start | Tab instead of spaces, or bad indentation |
| Schema mismatch | Config silently ignored | Field name typo or wrong nesting |
| Wrong model ID | `model_not_found` error | Model name changed or typo |
| Wrong base URL | `ECONNREFUSED` or `404` | URL has trailing slash or wrong path |
| Missing required field | Partial startup, some features disabled | Optional-looking field is actually required |

---

## Fix 1: Environment Variable Validation on Startup

Fail fast if required variables are missing:

```python
import os

REQUIRED_ENV_VARS = [
    'ANTHROPIC_API_KEY',
    'OPENCLAW_SESSION_SECRET',
    'DATABASE_URL',
]

def validate_env():
    missing = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"See .env.example for reference."
        )

validate_env()  # Call before anything else
```

Without this, agents often fail silently or with cryptic errors 10 minutes into a session.

---

## Fix 2: YAML Syntax — Common Pitfalls

```yaml
# BAD — tabs instead of spaces (YAML requires spaces)
providers:
	anthropic:           # Tab character → parse error
    model: claude-sonnet-4-6

# BAD — missing colon
providers:
  anthropic
    model: claude-sonnet-4-6  # SyntaxError

# BAD — wrong nesting
tools:
timeout_ms: 10000      # This sets a root-level key, not tools.timeout_ms

# GOOD
providers:
  anthropic:
    model: claude-sonnet-4-6
tools:
  timeout_ms: 10000
```

Validate your YAML before restarting:
```bash
python3 -c "import yaml; yaml.safe_load(open('openclaw.config.yaml'))"
# No output = valid YAML
```

---

## Fix 3: Model ID Reference

Model IDs change with new releases. Use the exact IDs:

```yaml
# Current model IDs (as of 2026)
providers:
  anthropic:
    models:
      fast: claude-haiku-4-5-20251001
      standard: claude-sonnet-4-6
      powerful: claude-opus-4-6
```

Common mistakes:
- `claude-3-opus` → should be `claude-opus-4-6`
- `claude-sonnet` → should be `claude-sonnet-4-6`
- `claude-haiku` → should be `claude-haiku-4-5-20251001`

---

## Fix 4: Base URL Configuration

```yaml
# BAD — trailing slash causes double-slash in paths
providers:
  anthropic:
    base_url: "https://api.anthropic.com/"  # → /v1//messages

# GOOD
providers:
  anthropic:
    base_url: "https://api.anthropic.com"   # No trailing slash

# For custom proxies or OpenAI-compatible endpoints
providers:
  custom:
    base_url: "https://your-proxy.example.com/v1"
    api_key: ${CUSTOM_API_KEY}
```

---

## Fix 5: `.env` File Not Loading

```bash
# .env file exists but vars aren't available
ANTHROPIC_API_KEY=sk-ant-xxx  # .env file

# BAD — .env not loaded by the runtime
openclaw start  # API key not available

# GOOD — explicit loading
source .env && openclaw start

# Or use dotenv in your startup script:
```

```python
from dotenv import load_dotenv
load_dotenv()  # Must be called BEFORE any env var access

import os
api_key = os.environ['ANTHROPIC_API_KEY']  # Now available
```

Common mistake: `.env` is loaded but the service is started by systemd or Docker, which doesn't source the file. Pass env vars explicitly:

```yaml
# docker-compose.yml
services:
  agent:
    env_file:
      - .env  # Docker loads this explicitly
```

---

## Fix 6: Config Schema Validation

Typos in field names are silently ignored — the config just uses the default:

```yaml
# BAD — typo, field silently ignored
agent:
  max_tokes: 4096        # "tokes" not "tokens" — ignored, uses default

# BAD — wrong nesting
temperature: 0.1         # Should be under providers.anthropic
providers:
  anthropic:
    model: claude-sonnet-4-6
```

Validate the schema:
```bash
openclaw config validate
# Lists all unknown fields and required-but-missing fields
```

---

## Fix 7: Secrets in Config Files

**Never put secrets in `openclaw.config.yaml`** — it gets committed to git.

```yaml
# BAD — secret in config file
providers:
  anthropic:
    api_key: sk-ant-api03-xxxx   # Will end up in git history

# GOOD — reference env var
providers:
  anthropic:
    api_key: ${ANTHROPIC_API_KEY}  # Loaded from environment at runtime
```

If you've already committed a secret:
```bash
# Rotate the key immediately — git history can't be easily purged
# Then remove from config and use env var going forward
```

---

## Fix 8: Provider-Specific Config Gotchas

### Anthropic

```yaml
providers:
  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
    # max_tokens is REQUIRED — Anthropic API requires it (no default)
    max_tokens: 8192
    # temperature: 0.0–1.0 (not 0.0–2.0 like OpenAI)
    temperature: 0.1
```

### OpenAI-compatible endpoints

```yaml
providers:
  openai:
    api_key: ${OPENAI_API_KEY}
    base_url: "https://api.openai.com/v1"
    # model must include full path for some providers
    model: "gpt-4o"
```

---

## Config Checklist

Before deploying:

- [ ] All required env vars documented in `.env.example`
- [ ] `.env` file in `.gitignore`
- [ ] No secrets in `openclaw.config.yaml`
- [ ] YAML syntax validated (`python3 -c "import yaml; yaml.safe_load(open('...'))"`)
- [ ] Model IDs verified against current provider documentation
- [ ] Base URLs have no trailing slashes
- [ ] `max_tokens` set explicitly (required by Anthropic API)
- [ ] Config schema validated (`openclaw config validate`)

---

[← View all config solutions](/synapse-ai/solutions/config/)

**Related guides:**
- [Auth Errors](/synapse-ai/guide/auth-errors) — API keys and OAuth config issues
- [Tool Failure Errors](/synapse-ai/guide/tool-failure-errors) — tool config and schema errors
- [Docker Errors](/synapse-ai/guide/docker-errors) — env var handling in containers

<div class="cta-box">
  <h3>Validate your agent config before it breaks</h3>
  <p>SynapseAI covers configuration error patterns from real OpenClaw deployments.</p>
  <code>clawhub install synapse-ai</code>
</div>
