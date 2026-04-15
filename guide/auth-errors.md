---
layout: default
title: "AI Agent Auth Errors — OAuth, 401, 403, Token Expiry Fixes"
description: "Fix OAuth, 401, 403, and token expiry errors in OpenClaw and AI agents. Covers token rotation, scope mismatch, credential caching, and re-auth loops."
permalink: /guide/auth-errors
---

# AI Agent Authentication Error Guide

Auth errors are the second most common source of token waste in OpenClaw. An agent caught in a re-auth loop or retrying failed API calls due to invalid credentials can burn 20,000–50,000 tokens before timing out or giving up.

---

## 401 Unauthorized

### 401 on Every Request Despite Valid Key

**Symptom:** All API calls return 401. Credentials verified working in curl. OpenClaw settings show correct key.

**Root cause:** OpenClaw caches auth headers at gateway startup. Updating credentials in settings does not propagate to the running gateway — it reads from the startup snapshot.

**Fix:**
```bash
openclaw gateway restart
```

If 401 persists after restart, verify the key is written correctly in `~/.openclaw/credentials.json`:
```bash
cat ~/.openclaw/credentials.json | grep api_key
```

Token budget saved: ~8,000 tokens vs. debugging the API call chain.

---

### 401 After Key Rotation

**Symptom:** Agent worked fine, then started failing with 401 after a key was rotated in the provider dashboard.

**Root cause:** Old key still in OpenClaw's credential store. The gateway doesn't poll for credential changes.

**Fix:**
```bash
openclaw config set anthropic.api_key "sk-ant-..."
openclaw gateway restart
```

---

### Pro Plan Not Recognized (401 on Pro-only features)

**Symptom:** 401 or 403 specifically on features that require a Pro subscription. Basic API calls work.

**Root cause:** The API key is from a Free or Team account. The session token doesn't carry subscription info; the key itself must be from a Pro account.

**Fix:** Generate a new API key from the Pro account (not a sub-account or team member key). [Full solution →](/synapse-ai/solutions/auth/)

---

## 403 Forbidden

### 403 on Dispatch / Cowork Endpoints

**Symptom:** `403 Request not allowed` on `/dispatch` or `/cowork` endpoints. Other endpoints work fine.

**Root cause:** These endpoints require specific OAuth scopes (`dispatch:write`, `cowork:read`). Standard API keys don't include them by default.

**Fix:** Re-authenticate with explicit scope request:
```yaml
oauth:
  scopes:
    - dispatch:write
    - cowork:read
    - agent:execute
```

Then revoke and regenerate the token. Old tokens don't gain new scopes retroactively.

---

### Slack MCP 403 — Cannot Reconnect

**Symptom:** Slack MCP server disconnects and cannot reconnect. Returns `403 channel_not_found` or `403 not_in_channel`.

**Root cause:** Slack bot token scope mismatch. The bot was removed from a channel or workspace permissions changed.

**Fix:**
1. Re-add the bot to the target channel: `/invite @your-bot`
2. If scope error: regenerate bot token with `channels:read`, `chat:write`, `im:history` scopes
3. Restart the MCP server connection

---

## OAuth Token Errors

### OAuth Refresh Token Silently Discarded

**Symptom:** Agent loses OAuth access every 1–2 hours. Logs show refresh attempt but new access token is never used.

**Root cause:** Some OAuth providers (Google, Notion) issue a new refresh token on each refresh. OpenClaw only stores the original. After the first refresh, the stored refresh token is invalid.

**Fix:**
```yaml
# openclaw.config.yaml
oauth:
  rotate_refresh_token: true
  persist_path: ~/.openclaw/oauth-tokens.json
```

Restart the gateway after this change. [Full solution →](/synapse-ai/solutions/auth/)

---

### OAuth Token Onboarding Has Multiple Failure Modes

**Symptom:** OAuth setup fails partway through. Error varies: sometimes redirect loop, sometimes `invalid_client`, sometimes silent failure.

**Root cause:** Three distinct failure points in the OAuth flow:
1. Redirect URI mismatch between registered app and OpenClaw config
2. Client secret expired or rotated
3. Browser session cookie conflict during redirect

**Fix by symptom:**
- `redirect_uri_mismatch` → Match exactly: `http://localhost:9991/oauth/callback`
- `invalid_client` → Regenerate client secret in provider dashboard
- Silent failure after redirect → Clear browser cookies for the provider domain, retry

---

### JWT Invalid in Edge Function

**Symptom:** `invalid JWT` error from Anthropic API when called from an edge function or serverless environment.

**Root cause:** JWT clock skew. Edge functions may run in environments where system time drifts. Anthropic JWTs have tight expiry windows.

**Fix:**
```javascript
// Add 30s buffer before expiry to force refresh
const EXPIRY_BUFFER_MS = 30000;
if (token.expiresAt - Date.now() < EXPIRY_BUFFER_MS) {
  token = await refreshToken();
}
```

---

### Auth Mode Switched from OAuth to API Key Without Notice

**Symptom:** Agent behavior changes unexpectedly. On inspection, auth mode switched from OAuth to API key, losing user-scoped permissions.

**Root cause:** OpenClaw falls back to API key auth if OAuth refresh fails silently. No notification is shown to the user or agent.

**Fix:** Add explicit auth mode enforcement:
```yaml
auth:
  mode: oauth  # Force OAuth — fail loudly rather than silent fallback
  on_oauth_failure: error  # Options: error | fallback | retry
```

---

## Authentication Loops

### Agent Stuck in Credential Reset Loop

**Symptom:** Agent repeatedly asks for credentials, resets, and asks again. Never completes setup.

**Root cause:** Credential validation endpoint returns 200 but with a body indicating invalid credentials. Agent parses only status code, not body.

**Fix:** Check credential validation response body, not just status:
```bash
curl -s -X POST https://your-provider/validate \
  -H "Authorization: Bearer $TOKEN" | jq '.valid'
# Should return: true
```

If `false` despite 200, the token is accepted syntactically but rejected semantically — generate a fresh token.

---

### Authentication Redirects to Onboarding for Existing Account

**Symptom:** Valid, active account redirected to onboarding flow on every login. Previous sessions lost.

**Root cause:** Session cookie domain mismatch or cookie cleared between sessions. The server-side session is valid but the client can't present the right cookie.

**Fix:**
1. Check cookie settings: `SameSite=None; Secure` required for cross-origin OAuth flows
2. Verify `url` in `_config.yml` or OpenClaw config exactly matches the host you're accessing
3. Clear all cookies for the domain and re-authenticate once

---

## Quick Reference

| Error | Most Likely Cause | First Fix to Try |
|-------|------------------|-----------------|
| 401 on all requests | Cached credentials after key change | `openclaw gateway restart` |
| 401 after key rotation | Old key in credential store | `openclaw config set` + restart |
| 403 on dispatch/cowork | Missing OAuth scope | Re-auth with required scopes |
| Refresh token invalid | Provider rotates tokens | Enable `rotate_refresh_token: true` |
| OAuth loop | Redirect URI mismatch | Match URI exactly in provider settings |
| JWT invalid | Clock skew in edge function | Add 30s expiry buffer |

---

[← View all 261+ auth solutions](/synapse-ai/solutions/auth/)

<div class="cta-box">
  <h3>Auto-resolve auth errors</h3>
  <p>SynapseAI detects common auth error patterns and suggests the right fix before your agent burns tokens on retries.</p>
  <code>clawhub install synapse-ai</code>
</div>
