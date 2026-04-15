---
layout: solution
title: "OAuth 400 Bad Request — redirect_uri Mismatch Between Client and Provider"
category: auth
description: "OAuth flow fails with 400 error and 'redirect_uri_mismatch' or 'invalid_redirect_uri'. The redirect URI in the request doesn't match the registered URI in the OAuth provider dashboard."
tags: [oauth, redirect-uri, 400, auth-flow, google, github]
---

# OAuth 400 Bad Request — redirect_uri Mismatch Between Client and Provider

## Symptom
- OAuth authorization flow fails with 400 or 401
- Error: `redirect_uri_mismatch`, `invalid_redirect_uri`, or `The redirect URI in the request did not match a registered redirect URI`
- Happens on: Google OAuth, GitHub OAuth, any OAuth 2.0 provider
- Works in one environment (local) but fails in another (production)
- Error appears at the OAuth provider's authorization page, not in your logs

## Root Cause
OAuth providers require the `redirect_uri` in the authorization request to match exactly one of the registered redirect URIs — including protocol, domain, port, and path. Common mismatches:

- `http://localhost:3000` vs `http://localhost:3000/` (trailing slash)
- `http://` vs `https://`
- `localhost` vs `127.0.0.1`
- Port present in request but not in registered URI
- Different path suffix (`/callback` vs `/auth/callback`)

## Fix

### Step 1: Check the exact URI being sent in the request

```python
from urllib.parse import urlencode, urlparse

# Log the exact URI being used in the OAuth request
redirect_uri = "https://your-app.com/auth/callback"
print(f"Using redirect_uri: {redirect_uri}")

# Build auth URL
params = {
    "client_id": CLIENT_ID,
    "redirect_uri": redirect_uri,
    "response_type": "code",
    "scope": "openid email profile",
}
auth_url = f"https://accounts.google.com/o/oauth2/auth?{urlencode(params)}"
print(f"Auth URL: {auth_url}")
```

### Step 2: Match the exact URI in the provider dashboard

For **Google OAuth**:
- Go to Google Cloud Console → Credentials → OAuth Client
- "Authorized redirect URIs" must match exactly
- Add both with and without trailing slash if needed:
  - `https://your-app.com/auth/callback`
  - `http://localhost:3000/auth/callback` (for local dev)

For **GitHub OAuth**:
- GitHub App settings → Authorization callback URL
- GitHub allows ONE redirect URI — use the production URI, handle dev separately

For **Generic OAuth provider**:
```bash
# Find the registered URIs vs what you're sending
curl "https://provider.com/.well-known/openid-configuration" | jq '.authorization_endpoint'
# Compare with what your code sends
```

### Step 3: Environment-aware redirect URI

```python
import os

def get_redirect_uri():
    env = os.environ.get("ENVIRONMENT", "development")
    base_url = {
        "production": "https://your-app.com",
        "staging": "https://staging.your-app.com",
        "development": "http://localhost:3000",
    }[env]
    return f"{base_url}/auth/callback"

# Use this everywhere — consistent URI construction
REDIRECT_URI = get_redirect_uri()
```

### Step 4: Register all environment URIs in the provider

For Google OAuth, add all variants at once:
```
https://your-app.com/auth/callback
https://staging.your-app.com/auth/callback
http://localhost:3000/auth/callback
http://127.0.0.1:3000/auth/callback
```

### Common Mismatch Fix Table

| Error | Registered | Fix |
|-------|-----------|-----|
| `http` vs `https` | `https://app.com/callback` | Update redirect_uri to use https |
| Trailing slash | `https://app.com/callback` | Remove trailing slash from request |
| Port difference | `https://app.com/callback` | Remove `:443` from request URI |
| Path difference | `https://app.com/callback` | Check for `/oauth/callback` vs `/callback` |

## Expected Token Savings
Debugging OAuth redirect URI mismatch: ~5,000 tokens
This fix: ~5 minutes of dashboard configuration

## Environment
- Google OAuth, GitHub OAuth, any OAuth 2.0 provider
- Most common when deploying to new environment (staging → production)
- Source: direct experience, OAuth 2.0 RFC 6749 §10.6
