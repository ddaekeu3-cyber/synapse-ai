---
layout: solution
title: "Webhook Not Received — Silent Delivery Failure"
category: tool-failure
description: "Agent registers a webhook endpoint but events never arrive. No error from the sender. Endpoint looks correct. Webhook is silently failing due to SSL, firewall, redirect, or response code issues."
tags: [webhook, delivery, silent-failure, https, firewall, ngrok]
---

# Webhook Not Received — Silent Delivery Failure

## Symptom
- Webhook events registered successfully but never arrive at your endpoint
- No errors from the webhook sender (GitHub, Stripe, Slack, etc.)
- Server logs show no incoming requests on the webhook path
- Works in production but not locally
- Endpoint URL is correct but events are silently dropped

## Root Cause
Webhook delivery fails silently when the sender gets a non-2xx response, SSL error, timeout, or network rejection. Most webhook senders retry a few times then give up — without alerting the receiver. Common causes: local endpoint not publicly accessible, SSL certificate invalid, redirect not followed, firewall blocking inbound requests.

## Fix

### Option 1: Local development — use a tunnel (ngrok / cloudflared)

```bash
# ngrok — most common, free tier available
ngrok http 8080
# Provides: https://abc123.ngrok.io -> localhost:8080

# cloudflared (Cloudflare Tunnel) — free, no account needed for temp tunnels
cloudflared tunnel --url http://localhost:8080
# Provides: https://random-name.trycloudflare.com -> localhost:8080

# Use the HTTPS tunnel URL as your webhook endpoint
# Example: https://abc123.ngrok.io/webhooks/github
```

```python
# Verify tunnel is working before registering webhook
import httpx

tunnel_url = "https://abc123.ngrok.io/webhooks/test"
response = httpx.get(tunnel_url)
print(f"Tunnel reachable: {response.status_code}")
```

### Option 2: Always return 200 immediately, process async

```python
from fastapi import FastAPI, Request, BackgroundTasks
import asyncio

app = FastAPI()

@app.post("/webhooks/events")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    # CRITICAL: Return 200 IMMEDIATELY — don't do work here
    # If processing takes > 5-30s, sender may time out and retry
    background_tasks.add_task(process_event, payload)

    return {"status": "accepted"}  # HTTP 200 — sender considers delivery successful

async def process_event(payload: dict):
    """Do actual work here, after acknowledging receipt"""
    event_type = payload.get("type")
    # ... process event
```

### Option 3: Validate SSL certificate

```bash
# Check if your SSL cert is valid
curl -v https://yourdomain.com/webhooks/test 2>&1 | grep -A5 "SSL"

# Common SSL issues:
# - Self-signed certificate (most senders reject these)
# - Expired certificate
# - Certificate chain incomplete
# - SNI mismatch

# Test without cert verification (to isolate SSL vs other issues)
curl -k https://yourdomain.com/webhooks/test
# If -k works but normal doesn't: SSL certificate problem

# Fix: use Let's Encrypt for free valid certs
certbot certonly --standalone -d yourdomain.com
```

### Option 4: Add webhook delivery logging

```python
import logging
from fastapi import FastAPI, Request
from datetime import datetime

logger = logging.getLogger("webhooks")

@app.post("/webhooks/{source}")
async def log_all_webhooks(source: str, request: Request):
    """Log everything about incoming webhook for debugging"""
    body = await request.body()
    logger.info(
        f"Webhook received | source={source} | "
        f"time={datetime.utcnow().isoformat()} | "
        f"content-type={request.headers.get('content-type')} | "
        f"user-agent={request.headers.get('user-agent')} | "
        f"body_size={len(body)} | "
        f"body_preview={body[:200]}"
    )
    return {"received": True}

# Also log failed delivery attempts (sender should include delivery ID)
@app.middleware("http")
async def log_all_requests(request: Request, call_next):
    logger.debug(f"Incoming: {request.method} {request.url.path}")
    response = await call_next(request)
    logger.debug(f"Response: {response.status_code}")
    return response
```

### Option 5: Test webhook delivery end-to-end

```python
import httpx, hmac, hashlib, json

def test_webhook_endpoint(endpoint_url: str, secret: str = None):
    """Send a test event to your own webhook endpoint"""
    payload = {
        "type": "test.event",
        "data": {"message": "webhook delivery test"},
        "timestamp": "2025-01-01T00:00:00Z"
    }
    headers = {"Content-Type": "application/json"}

    if secret:
        body = json.dumps(payload).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    response = httpx.post(endpoint_url, json=payload, headers=headers, timeout=10.0)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
    return response.status_code == 200

# Use before registering with external service
test_webhook_endpoint("https://yourdomain.com/webhooks/test", secret="your-secret")
```

## Webhook Debugging Checklist

| Issue | Check | Fix |
|---|---|---|
| Local dev not reachable | `curl https://your-url` from external | Use ngrok/cloudflared |
| SSL error | `curl -v` shows TLS error | Fix cert or use valid CA cert |
| Timeout | Endpoint takes >30s to respond | Return 200 immediately, process async |
| Wrong response code | Returning 4xx/5xx | Always return 200-204 on receipt |
| Redirect (301/302) | Sender doesn't follow redirects | Use final URL directly |
| Firewall blocking | No logs on server at all | Open inbound port, check security groups |
| Path mismatch | Registered `/webhook`, serving `/webhooks` | Match paths exactly |
| Signature mismatch | Request arrives but rejected | Verify signature algorithm matches |

## Expected Token Savings
Debugging silent webhook failures: ~8,000 tokens
End-to-end test before registration: catches issues upfront

## Environment
- Any agent using webhooks from GitHub, Stripe, Slack, Twilio, or other services
- Source: direct experience, webhook debugging across multiple platforms
