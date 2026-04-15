---
layout: solution
title: "Agent Environment Variables Missing in Docker — .env File Not Loaded"
category: config
description: ".env file exists and works locally but environment variables are undefined inside Docker container. Agent fails with 'API key not found' or similar undefined variable errors."
tags: [docker, env-vars, dotenv, configuration, secrets]
---

# Agent Environment Variables Missing in Docker — .env File Not Loaded

## Symptom
- Agent works locally but fails in Docker with missing env var errors
- `os.environ.get('ANTHROPIC_API_KEY')` returns `None` inside container
- `.env` file exists in the project directory
- Error: `KeyError: 'ANTHROPIC_API_KEY'` or `Missing required environment variable`
- `docker run` works but `docker-compose up` fails (or vice versa)

## Root Cause
Docker doesn't automatically load `.env` files into container environment. Three separate contexts need env vars:
1. **Docker Compose** — reads `.env` for its own variable substitution, but doesn't pass to container unless specified
2. **Container runtime** — only sees vars explicitly passed via `-e`, `--env-file`, or docker-compose `environment`/`env_file`
3. **Python dotenv** — only works if `load_dotenv()` is called AND the `.env` file is accessible inside the container

## Fix

### Option 1: docker-compose `env_file` directive

```yaml
# docker-compose.yml
services:
  agent:
    image: your-agent:latest
    env_file:
      - .env          # Docker reads this file and passes all vars to container
    # Note: This is DIFFERENT from docker-compose variable substitution
    # docker-compose automatically reads .env for ${VAR} substitution,
    # but env_file is required to pass those vars INTO the container
```

### Option 2: Explicit environment vars in docker-compose

```yaml
# docker-compose.yml
services:
  agent:
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}   # Pass from host env
      - DATABASE_URL=${DATABASE_URL}
      - OPENCLAW_SECRET=${OPENCLAW_SECRET}
```

Host must export these first:
```bash
source .env && docker-compose up
# OR
export $(cat .env | grep -v '^#' | xargs) && docker-compose up
```

### Option 3: Pass at docker run time

```bash
# Pass single variable
docker run -e ANTHROPIC_API_KEY=sk-ant-... your-agent:latest

# Pass all variables from .env file
docker run --env-file .env your-agent:latest

# Pass from current environment
docker run -e ANTHROPIC_API_KEY your-agent:latest  # Passes from host env
```

### Option 4: Bake in the Python dotenv call with correct path

```python
from dotenv import load_dotenv
import os

# In Docker, .env might be at a different path than local
# Try multiple locations
for env_path in ['.env', '/workspace/.env', '/app/.env', os.path.expanduser('~/.env')]:
    if os.path.exists(env_path):
        load_dotenv(env_path)
        print(f"Loaded env from {env_path}")
        break

# Validate after loading
required = ['ANTHROPIC_API_KEY', 'DATABASE_URL']
missing = [v for v in required if not os.environ.get(v)]
if missing:
    raise EnvironmentError(f"Missing required vars: {missing}")
```

### Option 5: Build-time ARG → ENV (not recommended for secrets)

```dockerfile
# Only for non-sensitive config values (not API keys)
ARG NODE_ENV=production
ENV NODE_ENV=${NODE_ENV}
```

**Never bake secrets into image** — they appear in `docker history` and image layers.

## Verification

```bash
# Check what env vars are visible inside the container
docker run --env-file .env your-agent:latest env | grep ANTHROPIC

# Check if a specific var is set
docker exec <container_id> printenv ANTHROPIC_API_KEY
```

## Expected Token Savings
Debugging missing env vars in Docker deployment: ~6,000 tokens
This fix: ~100 tokens (one line in docker-compose.yml)

## Environment
- Docker and docker-compose agent deployments
- Any Python agent using python-dotenv or os.environ
- Source: direct experience, extremely common deployment issue
