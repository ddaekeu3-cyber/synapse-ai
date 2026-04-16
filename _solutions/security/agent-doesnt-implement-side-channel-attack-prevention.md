---
title: "Agent Doesn't Implement Side-Channel Attack Prevention"
description: "AI agents leak information through timing differences, response length variations, and error message contents — allowing attackers to infer system prompt contents, internal state, or user data without direct access."
category: security
difficulty: advanced
tags: [side-channel, timing-attack, information-leakage, security, response-length, oracle, headers]
---

# Agent Doesn't Implement Side-Channel Attack Prevention

## Problem

Side-channel attacks extract information not from direct access to secrets, but from observable properties of responses: how long they take, how many tokens they contain, what error messages say, or which HTTP headers are set. An agent that responds faster to prompts that match its system prompt keywords leaks the system prompt's vocabulary through timing. One that returns longer responses when a condition is true creates a response-length oracle. These are real attack classes documented against production LLM deployments.

## Solution 1: Constant-Time String Comparison for Secrets

Use `hmac.compare_digest` instead of `==` for any comparison involving secrets, tokens, or API keys to prevent timing oracle attacks.

```python
import hashlib
import hmac
import secrets
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Bad: timing-variable comparison
def insecure_check(provided: str, expected: str) -> bool:
    return provided == expected  # short-circuits on first mismatch

# Good: constant-time comparison
def secure_check(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.encode(), expected.encode())

# Application: API key validation
API_KEY_HASH = hashlib.sha256(b"my-secret-api-key").hexdigest()

def validate_api_key(provided_key: str) -> bool:
    provided_hash = hashlib.sha256(provided_key.encode()).hexdigest()
    return hmac.compare_digest(provided_hash, API_KEY_HASH)

# Application: session token validation
def validate_session_token(token: str, stored_token: str) -> bool:
    return hmac.compare_digest(token, stored_token)

# Application: HMAC signature verification
def verify_webhook_signature(payload: bytes, signature: str, secret: bytes) -> bool:
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

# Timing attack demo: measure the difference
def timing_demo():
    secret = "correct_token_" + secrets.token_hex(16)
    wrong_prefix = "x" * len(secret)  # completely wrong
    close_guess = secret[:10] + "x" * (len(secret) - 10)  # first 10 chars correct

    N = 10000
    # Insecure: time varies based on match length
    t0 = time.perf_counter()
    for _ in range(N):
        insecure_check(wrong_prefix, secret)
    insecure_wrong = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(N):
        insecure_check(close_guess, secret)
    insecure_close = time.perf_counter() - t0

    # Secure: time is constant regardless of match length
    t0 = time.perf_counter()
    for _ in range(N):
        secure_check(wrong_prefix, secret)
    secure_wrong = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(N):
        secure_check(close_guess, secret)
    secure_close = time.perf_counter() - t0

    return {
        "insecure_timing_ratio": insecure_close / insecure_wrong,  # > 1.0 reveals info
        "secure_timing_ratio":   secure_close / secure_wrong,       # ≈ 1.0
    }

async def agent_with_secure_auth(api_key: str, user_message: str) -> dict:
    if not validate_api_key(api_key):
        # Add random jitter so timing doesn't reveal validity
        await __import__("asyncio").sleep(secrets.randbelow(50) / 1000)
        return {"error": "unauthorized"}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    return {"response": resp.content[0].text}
```

**When to use**: Any agent that compares tokens, API keys, session IDs, or HMAC signatures. Timing oracles are trivially exploitable at scale — `hmac.compare_digest` costs nothing.

---

## Solution 2: Response Normalization — Eliminate Length Oracle

Pad or normalize response lengths so attackers cannot infer internal state from response size.

```python
import asyncio
import json
import secrets
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def pad_response(response_body: str, target_multiple: int = 256) -> str:
    """
    Pad a JSON response to the next multiple of target_multiple bytes.
    Padding is added in a field that the client ignores.
    Prevents response-length oracle attacks.
    """
    current = len(response_body.encode("utf-8"))
    remainder = current % target_multiple
    if remainder == 0:
        return response_body

    pad_needed = target_multiple - remainder

    # Add padding as a field in the JSON response
    data = json.loads(response_body)
    data["_pad"] = "x" * (pad_needed - 10)  # -10 for field name overhead
    return json.dumps(data)

def add_response_jitter() -> float:
    """Return a random delay (ms) to add to response time."""
    return secrets.randbelow(50) / 1000  # 0–50ms uniform jitter

async def normalized_agent_response(user_message: str, is_authenticated: bool) -> dict:
    """
    Returns responses that don't leak auth status through timing or length.
    Both paths take similar time and return similar-size responses.
    """
    import asyncio

    if not is_authenticated:
        await asyncio.sleep(add_response_jitter())
        # Return a fixed-size error regardless of why auth failed
        response = {"error": "unauthorized", "code": 401}
        return json.loads(pad_response(json.dumps(response)))

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    await asyncio.sleep(add_response_jitter())

    response = {"response": resp.content[0].text}
    padded = pad_response(json.dumps(response), target_multiple=256)
    return json.loads(padded)

# Length oracle attack mitigation example:
# WITHOUT normalization:
#   "Is the system prompt about finance?" → 890 bytes (confirms it by triggering longer finance response)
#   "Is the system prompt about pets?"   → 312 bytes (shorter = not about pets)
#
# WITH normalization:
#   Both responses rounded to nearest 256 bytes → attacker cannot distinguish
```

**When to use**: Agents where the response length could reveal information about the system prompt or internal agent state (e.g., "tell me more about X" returning a longer response if X is in the system prompt).

---

## Solution 3: Error Message Sanitization — Don't Leak Internals in Errors

Sanitize all error messages before returning them. Internal exceptions (file paths, stack traces, SQL queries, system prompt fragments) must never reach the client.

```python
import asyncio
import logging
import traceback
import uuid
from anthropic import AsyncAnthropic, APIStatusError

client = AsyncAnthropic()
logger = logging.getLogger("agent.errors")

# Map exception types to safe, generic client messages
ERROR_MAP = {
    "APIStatusError":       "The AI service is temporarily unavailable.",
    "APITimeoutError":      "The request timed out. Please try again.",
    "APIConnectionError":   "Unable to reach the AI service.",
    "FileNotFoundError":    "A required resource is unavailable.",
    "PermissionError":      "Access denied.",
    "ValueError":           "Invalid request parameters.",
    "KeyError":             "Required data is missing.",
    "json.JSONDecodeError": "Data format error.",
}

def sanitize_error(exc: Exception) -> tuple[str, str]:
    """
    Returns (client_message, error_id).
    Logs the real exception internally; returns only a safe message to the client.
    The error_id lets support correlate client reports with server logs.
    """
    error_id = str(uuid.uuid4())[:8].upper()
    exc_type = type(exc).__name__

    # Log full exception internally (never sent to client)
    logger.error(
        "agent_error",
        extra={
            "error_id": error_id,
            "exc_type": exc_type,
            "exc_message": str(exc),
            "traceback": traceback.format_exc(),
        },
    )

    # Determine safe client message
    for key, message in ERROR_MAP.items():
        if key in exc_type:
            return message, error_id

    return "An unexpected error occurred.", error_id

async def safe_agent_endpoint(user_message: str, session_id: str) -> dict:
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        return {"response": resp.content[0].text, "session_id": session_id}

    except Exception as exc:
        client_message, error_id = sanitize_error(exc)
        return {
            "error": client_message,
            "error_id": error_id,  # safe to return — just an opaque ID
            "session_id": session_id,
        }

# Anti-patterns to avoid:
# NEVER: return {"error": str(exc)}
# NEVER: return {"error": traceback.format_exc()}
# NEVER: return {"error": f"File not found: {file_path}"}  ← leaks filesystem layout
# NEVER: return {"error": f"SQL error: {query}"}           ← leaks schema
# NEVER: return {"error": f"System prompt: {system[:100]}"} ← leaks prompt
```

**When to use**: All production agents. Error message leakage is the #1 source of inadvertent information disclosure in web applications, and LLM agents are especially vulnerable because exceptions often contain prompt content.

---

## Solution 4: HTTP Header Scrubbing — Don't Expose Server Internals

Strip or replace HTTP response headers that reveal agent implementation details (framework, version, internal hostnames).

```python
import asyncio
from typing import Callable, Awaitable

# Headers that reveal implementation details and should be removed/replaced
HEADERS_TO_REMOVE = frozenset({
    "X-Powered-By",
    "Server",
    "X-AspNet-Version",
    "X-AspNetMvc-Version",
    "X-Generator",
    "X-Runtime",
    "X-Rack-Cache",
    "Via",               # reveals proxy infrastructure
    "X-Cache",           # reveals caching layer
    "X-Cache-Hits",
    "X-Amz-RequestId",  # reveals AWS usage
    "X-Amz-Id-2",
    "CF-Ray",            # reveals Cloudflare usage
    "X-Served-By",
})

# Headers to add for security
SECURITY_HEADERS = {
    "X-Content-Type-Options":    "nosniff",
    "X-Frame-Options":           "DENY",
    "X-XSS-Protection":          "1; mode=block",
    "Referrer-Policy":           "strict-origin-when-cross-origin",
    "Permissions-Policy":        "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy":   "default-src 'self'",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "Cache-Control":             "no-store",  # don't cache agent responses
    "Server":                    "Agent",      # replace, not remove, for RFC compliance
}

def scrub_response_headers(headers: dict[str, str]) -> dict[str, str]:
    """
    Remove information-leaking headers and add security headers.
    """
    cleaned = {
        k: v for k, v in headers.items()
        if k not in HEADERS_TO_REMOVE
    }
    cleaned.update(SECURITY_HEADERS)
    return cleaned

# ASGI middleware for frameworks like FastAPI / Starlette
class HeaderScrubbingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_scrubbed_headers(message):
            if message["type"] == "http.response.start":
                raw_headers = dict(message.get("headers", []))
                # Convert bytes keys/values
                str_headers = {
                    k.decode() if isinstance(k, bytes) else k:
                    v.decode() if isinstance(v, bytes) else v
                    for k, v in raw_headers.items()
                }
                scrubbed = scrub_response_headers(str_headers)
                message["headers"] = [
                    (k.encode(), v.encode()) for k, v in scrubbed.items()
                ]
            await send(message)

        await self.app(scope, receive, send_with_scrubbed_headers)

# Example: what headers reveal
LEAKY_HEADERS_EXAMPLE = {
    "Server": "uvicorn/0.24.0",           # reveals framework + version
    "X-Powered-By": "FastAPI 0.104.1",    # reveals framework
    "Via": "1.1 internal-proxy.corp.lan", # reveals internal hostname
    "X-Amz-RequestId": "abc123",          # reveals cloud provider
}
# After scrubbing: {"Server": "Agent", + security headers only}
```

**When to use**: All web-facing agents. Information in HTTP headers enables fingerprinting attacks that help attackers choose exploits targeting your specific framework version.

---

## Solution 5: Prompt Content Firewall — Prevent System Prompt Exfiltration via Responses

The model may reveal system prompt content if asked directly or indirectly. Add a post-response scan that detects and blocks responses containing system prompt fragments.

```python
import asyncio
import re
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class SystemPromptFirewall:
    """
    Scans LLM responses for fragments of the system prompt before returning to client.
    Blocks any response that appears to echo back confidential system prompt content.
    """

    def __init__(self, system_prompt: str, min_fragment_length: int = 20):
        self._system_prompt = system_prompt
        self._min_len = min_fragment_length
        # Pre-compute n-grams of the system prompt for efficient matching
        self._sensitive_fragments = self._extract_sensitive_fragments(system_prompt)

    def _extract_sensitive_fragments(self, text: str) -> list[str]:
        """
        Extract overlapping fragments of min_fragment_length characters.
        Only keep fragments that are likely unique to the system prompt
        (not common English phrases).
        """
        fragments = []
        words = text.split()
        # Sliding window of 5+ words
        for i in range(len(words)):
            for j in range(i + 5, min(i + 20, len(words) + 1)):
                fragment = " ".join(words[i:j])
                if len(fragment) >= self._min_len:
                    fragments.append(fragment.lower())
        return fragments

    def scan(self, response_text: str) -> tuple[bool, str | None]:
        """
        Returns (is_safe, matched_fragment).
        is_safe=False means the response contains system prompt content.
        """
        response_lower = response_text.lower()
        for fragment in self._sensitive_fragments:
            if fragment in response_lower:
                return False, fragment
        return True, None

    def redact(self, response_text: str) -> str:
        """Replace detected fragments with a safe placeholder."""
        redacted = response_text
        for fragment in self._sensitive_fragments:
            pattern = re.compile(re.escape(fragment), re.IGNORECASE)
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

SYSTEM_PROMPT = """You are an AI assistant for AcmeCorp's internal HR platform.
You have access to employee records, compensation data, and organizational charts.
Your API key is acme-internal-hr-2024-REDACT.
You must never discuss competitor companies."""

firewall = SystemPromptFirewall(SYSTEM_PROMPT)

async def firewalled_agent(user_message: str) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    response_text = resp.content[0].text
    is_safe, matched = firewall.scan(response_text)

    if not is_safe:
        import logging
        logging.getLogger("security").warning(
            "system_prompt_leak_detected",
            extra={"matched_fragment": matched[:50], "user_message": user_message[:100]},
        )
        return {
            "response": "I can't share information about my configuration.",
            "security_event": "prompt_leak_blocked",
        }

    return {"response": response_text}
```

**When to use**: Agents with confidential system prompts (API keys, internal URLs, proprietary instructions). The firewall catches prompt leakage from both direct extraction attempts and indirect leakage.

---

## Solution 6: Request Timing Normalization — Prevent Timing Oracle via Response Latency

Add controlled jitter to all responses so that an attacker measuring latency cannot distinguish "fast path (not found)" from "slow path (found, processing)."

```python
import asyncio
import secrets
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class TimingNormalizer:
    """
    Ensures all responses take at least `min_ms` milliseconds.
    Adds random jitter up to `jitter_ms` to prevent timing fingerprinting.
    """

    def __init__(self, min_ms: float = 100.0, jitter_ms: float = 50.0):
        self.min_ms = min_ms
        self.jitter_ms = jitter_ms

    async def normalize(self, coro) -> any:
        """
        Run coro; pad response time to at least min_ms + random jitter.
        """
        target_ms = self.min_ms + secrets.randbelow(int(self.jitter_ms * 1000)) / 1000
        start = time.monotonic()

        result = await coro

        elapsed_ms = (time.monotonic() - start) * 1000
        remaining_ms = target_ms - elapsed_ms
        if remaining_ms > 0:
            await asyncio.sleep(remaining_ms / 1000)

        return result

normalizer = TimingNormalizer(min_ms=200, jitter_ms=100)

async def _do_auth_check(user_id: str, token: str) -> bool:
    """Simulated auth check — fast if user not found, slow if checking password."""
    # In reality: database lookup (fast if not found, hash compare if found)
    await asyncio.sleep(0.05 if user_id == "unknown" else 0.15)
    return user_id == "alice" and token == "correct"

async def timing_safe_auth(user_id: str, token: str, user_message: str) -> dict:
    # Without normalization: 50ms = user doesn't exist, 150ms = password wrong
    # With normalization: always 200–300ms regardless
    is_valid = await normalizer.normalize(_do_auth_check(user_id, token))

    if not is_valid:
        return {"error": "unauthorized"}

    result = await normalizer.normalize(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": user_message}],
        )
    )
    return {"response": result.content[0].text}

# Timing oracle attack example (without normalization):
# Attacker sends 1000 requests with random user IDs, measures latency
# Short latency → user doesn't exist (enumerate valid users)
# Long latency  → user exists (valid user ID confirmed)
# With TimingNormalizer: all responses normalized to 200–300ms → no information leakage
```

**When to use**: Agent authentication endpoints, user lookup endpoints, and any endpoint where different internal code paths have measurably different latencies. Timing normalization is especially important for user enumeration prevention.

---

## Comparison

| Solution | Attack Type Prevented | Overhead | Complexity | Detection | Best For |
|---|---|---|---|---|---|
| Constant-time comparison | Timing oracle on secrets | ~0% | Low | No | All token/secret comparisons |
| Response length normalization | Length oracle | ~1% (padding) | Low | No | Confidential state agents |
| Error message sanitization | Information disclosure | ~0% | Low | Via logs | All production agents |
| HTTP header scrubbing | Fingerprinting | ~0% | Low | No | Web-facing agents |
| Prompt content firewall | System prompt exfiltration | 2–5% | High | Yes | Confidential system prompts |
| Timing normalization | Timing oracle on latency | ≤ target_ms | Medium | No | Auth endpoints, user lookup |

**Rule of thumb**: Apply constant-time comparison (Solution 1) and error sanitization (Solution 3) everywhere — they cost nothing. Add timing normalization (Solution 6) to auth paths. Add the prompt firewall (Solution 5) only when the system prompt contains secrets or proprietary instructions.
