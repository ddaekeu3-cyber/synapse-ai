---
layout: solution
title: "Lambda Agent Slow on First Request — Cold Start Latency"
category: performance
description: "Agent deployed on AWS Lambda takes 8-15 seconds for the first request after idle. Subsequent requests are fast (< 1s). Cold start loading large ML models or initializing heavy clients causes the delay."
tags: [lambda, cold-start, performance, aws, serverless, latency]
---

# Lambda Agent Slow on First Request — Cold Start Latency

## Symptom
- First request after deployment or idle period takes 8–30 seconds
- Subsequent requests complete in under 1 second
- Users see timeout errors on the first request
- Pattern: slow → fast → fast → ... → (idle 15 min) → slow again
- CloudWatch logs show most of the latency in `INIT` phase

## Root Cause
Lambda executes initialization code on every cold start:
1. Download and unzip deployment package
2. Start runtime (Python interpreter)
3. Import modules and initialize global state
4. Your handler is finally called

Large packages (ML libraries, heavy SDKs), big models loaded into memory, or slow network calls during init all compound cold start time.

## Fix

### Option 1: Move initialization outside the handler (module level)

```python
import anthropic, os

# WRONG — initialized inside handler: runs on EVERY invocation
def handler(event, context):
    client = anthropic.Anthropic()  # Re-initialized every call!
    response = client.messages.create(...)
    return response

# RIGHT — initialized at module level: runs once per container lifetime
client = anthropic.Anthropic()  # Initialized on cold start, reused on warm starts

def handler(event, context):
    response = client.messages.create(  # Reuses warm client
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": event["message"]}]
    )
    return {"response": response.content[0].text}
```

### Option 2: Reduce package size — exclude unused dependencies

```bash
# Check what's making your package large
pip install pipdeptree
pipdeptree --warn silence | head -50

# Common space hogs to exclude:
# - boto3 (already available in Lambda runtime — don't bundle it)
# - tests/ directories
# - *.dist-info directories

# .dockerignore / deployment exclusions:
# __pycache__
# *.pyc
# tests/
# docs/
# *.dist-info

# Use Lambda layers for large shared dependencies
# (shared across functions, cached separately)
```

```yaml
# serverless.yml or SAM — use Lambda layers
layers:
  - arn:aws:lambda:us-east-1:123456789:layer:anthropic-sdk:1

# Or define your own layer
package:
  exclude:
    - boto3/**
    - botocore/**
    - .git/**
    - tests/**
```

### Option 3: Provisioned concurrency — keep containers warm

```yaml
# AWS SAM template.yaml
Resources:
  AgentFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: handler.handler
      Runtime: python3.12
      MemorySize: 512
      Timeout: 30
      AutoPublishAlias: live
      ProvisionedConcurrencyConfig:
        ProvisionedConcurrentExecutions: 2  # Keep 2 warm containers
```

```bash
# AWS CLI
aws lambda put-provisioned-concurrency-config \
    --function-name my-agent \
    --qualifier live \
    --provisioned-concurrent-executions 2
```

Cost note: Provisioned concurrency charges even when idle. Use only for latency-critical paths.

### Option 4: Use Lambda SnapStart (Java) or container image caching

```python
# For Python: use container images with pre-cached layers
# Dockerfile for Lambda container image

FROM public.ecr.aws/lambda/python:3.12

# Install dependencies (cached in image layer)
COPY requirements.txt .
RUN pip install -r requirements.txt --target /var/task

# Pre-download any models or warm up connections
# Run during docker build, not at Lambda init time
RUN python3 -c "import anthropic; print('SDK loaded')"

COPY . /var/task/
CMD ["handler.handler"]
```

### Option 5: Lazy initialization with caching

```python
import anthropic, functools, time

_client = None
_client_created_at = 0
CLIENT_TTL = 3600  # Re-create client after 1 hour

def get_client() -> anthropic.Anthropic:
    """Lazy init with TTL — avoids re-init during warm invocations"""
    global _client, _client_created_at
    now = time.time()
    if _client is None or (now - _client_created_at) > CLIENT_TTL:
        _client = anthropic.Anthropic()
        _client_created_at = now
    return _client

# Database connections — create connection pool at module level
import psycopg2.pool

_db_pool = None

def get_db():
    global _db_pool
    if _db_pool is None:
        _db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 5,
            os.environ["DATABASE_URL"]
        )
    return _db_pool.getconn()
```

### Option 6: Warm-up ping before user traffic

```python
# CloudWatch Events rule to ping Lambda every 5 minutes
# (prevents cold starts during business hours)

import json

def handler(event, context):
    # Handle CloudWatch warm-up ping
    if event.get("source") == "warmup":
        print("Warm-up ping received — container is ready")
        return {"status": "warm"}

    # Normal invocation
    return process_user_request(event)
```

```yaml
# SAM / CloudFormation — scheduled warm-up
WarmupRule:
  Type: AWS::Events::Rule
  Properties:
    ScheduleExpression: rate(5 minutes)
    Targets:
      - Arn: !GetAtt AgentFunction.Arn
        Input: '{"source": "warmup"}'
```

## Cold Start Latency by Factor

| Factor | Cold start contribution | Fix |
|---|---|---|
| Python runtime start | 200–500ms | Unavoidable |
| Package import time | 100ms–5s | Reduce package size |
| anthropic SDK init | ~50ms | Module-level init |
| Database connection | 100ms–3s | Connection pooling |
| Loading ML model | 5–30s | Lambda layer + cache |
| Network calls in init | Network RTT | Move to handler |

## Expected Token Savings
Not about token savings — about reducing user-visible latency from 15s to <1s.

## Environment
- AWS Lambda-deployed agents; also applies to Google Cloud Functions, Azure Functions
- Source: direct experience with serverless agent deployments
