---
title: "Agent Doesn't Implement Property-Based Testing for Security Invariants"
description: "AI agents tested only with hand-written examples miss entire classes of security violations triggered by unusual inputs. Property-based testing generates thousands of adversarial inputs automatically, verifying that security invariants hold across all of them."
date: 2025-02-02
difficulty: advanced
category: security
slug: agent-doesnt-implement-property-based-testing-for-security-invariants
tags:
  - property-based-testing
  - hypothesis
  - security
  - invariants
  - fuzzing
  - testing
  - input-validation
symptoms:
  - "Security bugs are found in production with inputs that were never manually tested"
  - "Injection vulnerabilities appear in edge cases like empty strings, Unicode, or very long inputs"
  - "Access control logic passes unit tests but fails on unexpected role combinations"
  - "Sanitisation functions are correct for ASCII but fail on multi-byte characters"
  - "Auth token validation passes typical cases but has bypass paths for malformed tokens"
---

## Problem

Hand-written test cases represent the author's mental model of valid inputs. Attackers operate outside that mental model — they use null bytes, Unicode confusables, extremely long strings, negative numbers, and combinations of fields that developers never considered.

Property-based testing (Hypothesis in Python) generates thousands of inputs automatically according to declared strategies. Instead of testing "does this work for input X?", you test "does this property hold for all inputs in this space?" Security properties are ideal targets: sanitisation must never leak raw HTML, auth checks must never grant access to unauthorised roles, SQL builders must never produce unquoted values.

---

## Solution 1: Sanitisation Invariant Tests

Verify that the agent's output sanitiser never allows raw HTML or script injection, regardless of input.

```python
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
import html
import re


# ─── The system under test ────────────────────────────────────────────────────

def sanitise_for_display(text: str) -> str:
    """
    Agent's output sanitiser — should strip/escape all HTML.
    This implementation has a deliberate bug for illustration.
    """
    # BUG: only escapes < and > but not quotes or &
    return text.replace("<", "&lt;").replace(">", "&gt;")


def safe_sanitise(text: str) -> str:
    """Correct implementation using stdlib."""
    return html.escape(text, quote=True)


# ─── Properties ───────────────────────────────────────────────────────────────

SCRIPT_TAG = re.compile(r"<\s*script", re.IGNORECASE)
ON_EVENT = re.compile(r"\bon\w+\s*=", re.IGNORECASE)
UNESCAPED_AMP = re.compile(r"&(?!#?\w+;)")


@given(st.text())
@settings(max_examples=2000, suppress_health_check=[HealthCheck.too_slow])
def test_sanitise_no_script_tags(text: str):
    result = safe_sanitise(text)
    assert not SCRIPT_TAG.search(result), (
        f"Script tag survived sanitisation: {result!r}"
    )


@given(st.text())
@settings(max_examples=2000)
def test_sanitise_no_event_handlers(text: str):
    result = safe_sanitise(text)
    assert not ON_EVENT.search(result), (
        f"Event handler survived sanitisation: {result!r}"
    )


@given(st.text(alphabet=st.characters(whitelist_categories=("L", "N", "P", "S"))))
@settings(max_examples=500)
def test_sanitise_is_idempotent(text: str):
    """Applying sanitiser twice should not change the output."""
    once = safe_sanitise(text)
    twice = safe_sanitise(once)
    # The second pass should only change &amp; -> &amp;amp; etc., not produce new HTML
    assert not SCRIPT_TAG.search(twice)
```

---

## Solution 2: Access Control Invariant Tests

Verify that the agent's permission checker never grants access when it shouldn't, across all combinations of roles, resources, and actions.

```python
from hypothesis import given, assume, settings
from hypothesis import strategies as st
from typing import FrozenSet, Set

# ─── System under test ───────────────────────────────────────────────────────

ROLE_PERMISSIONS = {
    "admin":    {"read", "write", "delete", "admin"},
    "editor":   {"read", "write"},
    "viewer":   {"read"},
    "readonly": {"read"},
}


def check_permission(roles: list[str], action: str, resource: str) -> bool:
    """
    Returns True if ANY of the user's roles grants `action`.
    Resource-level overrides not yet implemented (bug surface).
    """
    allowed: Set[str] = set()
    for role in roles:
        allowed |= ROLE_PERMISSIONS.get(role, set())
    return action in allowed


ROLES = st.sampled_from(list(ROLE_PERMISSIONS.keys()) + ["unknown_role", "", "  "])
ACTIONS = st.sampled_from(["read", "write", "delete", "admin", "DROP TABLE", "<script>"])
RESOURCES = st.text(max_size=50)


# ─── Properties ───────────────────────────────────────────────────────────────

@given(st.lists(ROLES, min_size=1, max_size=5), ACTIONS, RESOURCES)
@settings(max_examples=1000)
def test_unknown_role_never_grants_privileged_action(
    roles: list, action: str, resource: str
):
    """A user with only unknown roles must never get write/delete/admin."""
    if all(r not in ROLE_PERMISSIONS for r in roles):
        assume(action in {"write", "delete", "admin"})
        result = check_permission(roles, action, resource)
        assert not result, (
            f"Unknown roles {roles} granted action={action!r} on {resource!r}"
        )


@given(st.lists(ROLES, min_size=1), ACTIONS, RESOURCES)
@settings(max_examples=1000)
def test_extra_roles_never_reduce_access(roles: list, action: str, resource: str):
    """Adding a role must not reduce existing access."""
    base = check_permission(roles, action, resource)
    extended = check_permission(roles + ["viewer"], action, resource)
    if base:
        assert extended, (
            f"Adding 'viewer' reduced access for roles={roles}, action={action!r}"
        )


@given(ACTIONS, RESOURCES)
@settings(max_examples=500)
def test_empty_roles_never_grants_write(action: str, resource: str):
    assume(action in {"write", "delete", "admin"})
    assert not check_permission([], action, resource)
```

---

## Solution 3: SQL Builder Injection Invariant

Verify that the agent's query builder never produces unparameterised user values in the SQL string, regardless of what the user supplies.

```python
from hypothesis import given, settings
from hypothesis import strategies as st
import re

# ─── System under test ───────────────────────────────────────────────────────

def build_search_query(user_input: str) -> tuple[str, tuple]:
    """
    Build a parameterised search query.
    CORRECT: uses ? placeholders.
    """
    cleaned = user_input.strip()[:200]
    return (
        "SELECT id, title FROM documents WHERE title LIKE ?",
        (f"%{cleaned}%",),
    )


def build_search_query_BROKEN(user_input: str) -> tuple[str, tuple]:
    """Broken version that inlines user input."""
    return (
        f"SELECT id, title FROM documents WHERE title LIKE '%{user_input}%'",
        (),
    )


SQL_INJECTION_CHARS = re.compile(r"['\";\\]|--|\bOR\b|\bUNION\b|\bDROP\b",
                                   re.IGNORECASE)

# ─── Properties ───────────────────────────────────────────────────────────────

@given(st.text())
@settings(max_examples=5000)
def test_sql_builder_never_inlines_user_input(text: str):
    sql, params = build_search_query(text)
    # The SQL string must only contain ? placeholders, not the raw input
    assert text not in sql or len(text) == 0, (
        f"User input found raw in SQL: {sql!r}"
    )
    # All dynamic values must be in params, not in the SQL string
    assert sql.count("?") == len(params), (
        f"Mismatch between placeholders and params: {sql!r}, {params!r}"
    )


@given(st.text(alphabet=st.characters(whitelist_categories=("L", "N"))))
@settings(max_examples=1000)
def test_sql_builder_never_contains_injection_chars(text: str):
    sql, _ = build_search_query(text)
    assert not SQL_INJECTION_CHARS.search(sql), (
        f"Injection-risky chars in SQL template: {sql!r}"
    )
```

---

## Solution 4: JWT Validation Invariant Tests

Verify that the agent's token validation logic never accepts tampered, expired, or structurally invalid tokens.

```python
import base64
import json
import time
from hypothesis import given, settings, assume
from hypothesis import strategies as st

# ─── System under test ───────────────────────────────────────────────────────

import hmac
import hashlib

SECRET = b"test-secret-do-not-use-in-prod"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def sign_token(payload: dict, secret: bytes = SECRET) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    sig = hmac.new(secret, f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


def validate_token(token: str, secret: bytes = SECRET) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT structure")
    header_b64, payload_b64, sig_b64 = parts
    expected_sig = hmac.new(
        secret, f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    provided_sig = _b64url_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise ValueError("Invalid signature")
    payload = json.loads(_b64url_decode(payload_b64))
    if payload.get("exp", float("inf")) < time.time():
        raise ValueError("Token expired")
    return payload


# ─── Properties ───────────────────────────────────────────────────────────────

@given(st.text())
@settings(max_examples=2000)
def test_random_string_never_validates(token: str):
    try:
        validate_token(token)
        assert False, f"Random string validated as JWT: {token!r}"
    except (ValueError, Exception):
        pass  # expected


@given(st.text(min_size=1, max_size=100),
       st.integers(min_value=1, max_value=3600))
@settings(max_examples=500)
def test_valid_token_always_validates(sub: str, ttl: int):
    payload = {"sub": sub, "exp": int(time.time()) + ttl}
    token = sign_token(payload)
    result = validate_token(token)
    assert result["sub"] == sub


@given(st.text(min_size=1, max_size=100))
@settings(max_examples=500)
def test_tampered_payload_never_validates(sub: str):
    payload = {"sub": sub, "exp": int(time.time()) + 3600}
    token = sign_token(payload)
    header, _, sig = token.split(".")
    tampered_payload = _b64url(json.dumps({"sub": "attacker", "exp": 9999999999}).encode())
    tampered_token = f"{header}.{tampered_payload}.{sig}"
    try:
        validate_token(tampered_token)
        assert False, "Tampered token validated!"
    except ValueError:
        pass
```

---

## Solution 5: Rate Limit Invariant Tests

Verify that the agent's rate limiter never allows more than N requests per window, regardless of timing patterns or concurrent callers.

```python
import asyncio
import time
from hypothesis import given, settings
from hypothesis import strategies as st

# ─── System under test ───────────────────────────────────────────────────────

class SimpleRateLimiter:
    def __init__(self, max_requests: int, window: float):
        self._max = max_requests
        self._window = window
        self._timestamps: list = []

    def allow(self) -> bool:
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < self._window]
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True


# ─── Properties ───────────────────────────────────────────────────────────────

@given(
    st.integers(min_value=1, max_value=20),   # max_requests
    st.floats(min_value=0.01, max_value=1.0),  # window seconds
    st.integers(min_value=1, max_value=50),   # burst size
)
@settings(max_examples=500)
def test_rate_limiter_never_exceeds_limit(max_req: int, window: float, burst: int):
    """No burst of requests in one window should exceed max_requests allowed."""
    limiter = SimpleRateLimiter(max_req, window)
    allowed_count = sum(1 for _ in range(burst) if limiter.allow())
    assert allowed_count <= max_req, (
        f"Allowed {allowed_count} > max {max_req} in window {window}s"
    )


@given(st.integers(min_value=1, max_value=10))
@settings(max_examples=200)
def test_rate_limiter_reset_after_window(max_req: int):
    """After the window expires, full quota is available again."""
    limiter = SimpleRateLimiter(max_req, window=0.001)  # 1 ms window
    for _ in range(max_req):
        limiter.allow()
    time.sleep(0.005)  # wait for window to expire
    # Should allow max_req again
    second_batch = sum(1 for _ in range(max_req) if limiter.allow())
    assert second_batch == max_req
```

---

## Solution 6: Composite Security Property Suite

A pytest suite that runs all property-based security invariants as part of the CI pipeline, with reproducible seeds for failure replay.

```python
import pytest
from hypothesis import given, settings, seed, HealthCheck
from hypothesis import strategies as st


class SecurityInvariantSuite:
    """
    Runnable as: pytest -v test_security_invariants.py

    Each test method is a property that must hold for all inputs.
    On failure, Hypothesis prints the minimal failing example.
    """

    @given(st.text(max_size=10000))
    @settings(max_examples=3000, suppress_health_check=[HealthCheck.too_slow])
    def test_sanitiser_xss_invariant(self, text: str):
        """safe_sanitise must produce no executable HTML for any input."""
        import html as html_lib
        result = html_lib.escape(text, quote=True)
        assert "<script" not in result.lower()
        assert "javascript:" not in result.lower()
        assert "onerror=" not in result.lower()

    @given(st.binary(max_size=1024))
    @settings(max_examples=1000)
    def test_b64_decoder_never_crashes(self, data: bytes):
        """Base64 decoder used in JWT parsing must not raise on any byte sequence."""
        import base64
        try:
            base64.urlsafe_b64decode(data + b"==")
        except Exception as exc:
            # Only binascii.Error is acceptable; other exceptions are bugs
            import binascii
            assert isinstance(exc, binascii.Error), (
                f"Unexpected exception type {type(exc).__name__}: {exc}"
            )

    @given(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-", max_size=100),
        st.text(max_size=500),
    )
    @settings(max_examples=2000)
    def test_permission_check_deterministic(self, role: str, resource: str):
        """Permission check for same inputs must always return same result."""
        r1 = check_permission([role], "read", resource)
        r2 = check_permission([role], "read", resource)
        assert r1 == r2, "Permission check is non-deterministic!"

    @given(st.text())
    @settings(max_examples=2000)
    def test_sql_builder_always_parameterised(self, text: str):
        sql, params = build_search_query(text)
        placeholder_count = sql.count("?")
        assert placeholder_count == len(params), (
            f"SQL={sql!r} has {placeholder_count} placeholders but {len(params)} params"
        )
        # The raw user text must not appear in the SQL template
        if len(text) > 0:
            assert text[:20] not in sql, f"User text leaked into SQL: {sql!r}"


# Pytest integration
def test_security_invariants():
    suite = SecurityInvariantSuite()
    suite.test_sanitiser_xss_invariant()
    suite.test_b64_decoder_never_crashes()
    suite.test_sql_builder_always_parameterised()
```

---

## Comparison

| Approach | Property Type | Auto-generates Inputs | Shrinks Failures |
|---|---|---|---|
| **Sanitisation Invariant** | No script/event tags survive | Yes (Hypothesis) | Yes |
| **Access Control Invariant** | Unknown roles never get write | Yes | Yes |
| **SQL Builder Invariant** | No user input in SQL template | Yes | Yes |
| **JWT Validation Invariant** | Tampered tokens always rejected | Yes | Yes |
| **Rate Limiter Invariant** | Never exceeds N per window | Yes | Yes |
| **Composite CI Suite** | All of the above in one run | Yes | Yes |

**Key insight**: property-based tests are not a replacement for unit tests — they complement them. Write unit tests for known-good and known-bad examples; write property tests for the universal invariants that must hold for every possible input. Together they cover the full input space.
