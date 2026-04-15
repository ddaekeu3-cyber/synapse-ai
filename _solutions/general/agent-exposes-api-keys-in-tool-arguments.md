---
layout: solution
title: "Agent Exposes API Keys in Tool Arguments"
category: general
description: "The agent passes raw API keys, passwords, or bearer tokens as tool arguments — leaking secrets into conversation history, logs, and tool result traces visible to anyone with access to the session."
tags: [security, secrets, api-keys, credentials, environment-variables, vault, redaction]
---

## Symptom

A user asks the agent to "use my Stripe key to check the balance." The agent calls `check_stripe_balance(api_key="sk-live-REAL-KEY-HERE")`. The key is now in:
- The conversation history returned by the API
- Server-side logs
- Any tool result trace or debug output
- The context window, available to future turns

Or worse: the agent extracts credentials from user messages and passes them through multiple tool calls, creating a trail of exposed secrets.

## Root Cause

Tool arguments are plain JSON — they appear in API responses, logs, and history in cleartext. If the agent receives a secret in user input or generates one, and passes it as a tool argument, that secret is permanently embedded in the conversation trail. There is no built-in secret masking in the Anthropic SDK.

## Fix

---

### Option 1: Secret Injection at Execution Time — Never in Arguments

Keep secrets in environment variables. Tool implementations inject credentials themselves; the LLM never sees them.

```python
import os
import json
import anthropic

client = anthropic.Anthropic()

# Secrets live here — never in tool arguments
SECRET_STORE = {
    "stripe":    os.environ.get("STRIPE_API_KEY", "sk-test-placeholder"),
    "sendgrid":  os.environ.get("SENDGRID_API_KEY", "SG.placeholder"),
    "github":    os.environ.get("GITHUB_TOKEN", "ghp_placeholder"),
    "openai":    os.environ.get("OPENAI_API_KEY", "sk-placeholder"),
}

# Tool schema intentionally has NO api_key parameter
TOOLS = [
    {
        "name": "get_stripe_balance",
        "description": "Get Stripe account balance. Credentials are managed server-side.",
        "input_schema": {
            "type": "object",
            "properties": {
                "currency": {
                    "type": "string",
                    "default": "usd",
                    "description": "Currency code (e.g., usd, eur)",
                },
            },
        },
    },
    {
        "name": "send_email",
        "description": "Send an email via SendGrid. Credentials are managed server-side.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
                "body":    {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "get_github_repos",
        "description": "List GitHub repositories for the authenticated account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "org": {"type": "string", "description": "Organization name (optional)"},
            },
        },
    },
]


def execute_tool(tool_name: str, args: dict) -> dict:
    """
    Execute tool with server-side credential injection.
    The LLM never sees the actual keys.
    """
    if tool_name == "get_stripe_balance":
        api_key = SECRET_STORE["stripe"]  # injected here, not from LLM
        # stripe.api_key = api_key
        # balance = stripe.Balance.retrieve()
        return {
            "available": [{"amount": 10000, "currency": args.get("currency", "usd")}],
            "pending":   [{"amount": 500,   "currency": args.get("currency", "usd")}],
        }

    elif tool_name == "send_email":
        api_key = SECRET_STORE["sendgrid"]  # injected here
        # sendgrid.send(api_key=api_key, ...)
        return {"sent": True, "to": args["to"], "message_id": "msg_abc123"}

    elif tool_name == "get_github_repos":
        token = SECRET_STORE["github"]  # injected here
        # headers = {"Authorization": f"token {token}"}
        return {"repos": ["my-project", "api-client", "utils"], "count": 3}

    return {"error": f"Unknown tool: {tool_name}"}


def run_secure_agent(user_message: str) -> str:
    """
    The LLM selects tools and parameters.
    Credentials are NEVER part of tool arguments.
    """
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                # Verify no secret-like values slipped into args
                for key, value in block.input.items():
                    if isinstance(value, str) and (
                        value.startswith(("sk_", "SG.", "ghp_", "Bearer ", "pk_")) or
                        len(value) > 40 and "_" in value
                    ):
                        print(f"  ⚠️ Potential secret in tool arg '{key}' — stripping")
                        block.input[key] = "[REDACTED]"

                result = execute_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(run_secure_agent("Check my Stripe balance in USD and then get my GitHub repos."))
```

**Expected Token Savings**: Zero token cost — credentials never enter the context. No remediation tokens needed for leaked-key cleanup.
**Environment**: Environment variables via `os.environ`. Use a secrets manager (AWS Secrets Manager, HashiCorp Vault) for production.

---

### Option 2: Secret Reference System — Pass Names, Not Values

Allow the LLM to reference credentials by name (e.g., `"credential_name": "stripe_prod"`). Never the actual key.

```python
import os
import json
import re
import anthropic

client = anthropic.Anthropic()

# Registry of named credentials (values loaded from env/vault)
CREDENTIAL_REGISTRY = {
    "stripe_prod":    os.environ.get("STRIPE_API_KEY",  "sk-test-placeholder"),
    "stripe_test":    os.environ.get("STRIPE_TEST_KEY", "sk-test-placeholder2"),
    "aws_s3":         os.environ.get("AWS_SECRET_KEY",  "aws_placeholder"),
    "sendgrid_main":  os.environ.get("SENDGRID_KEY",    "SG.placeholder"),
}

TOOLS = [
    {
        "name": "make_api_call",
        "description": "Make an authenticated API call. Use a credential_name from the registry, never the actual key value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service":         {"type": "string", "description": "API service name (stripe, sendgrid, aws)"},
                "endpoint":        {"type": "string", "description": "API endpoint path"},
                "method":          {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
                "credential_name": {
                    "type": "string",
                    "description": "Name of the credential to use from the registry. Do NOT provide the actual key value.",
                    "enum": list(CREDENTIAL_REGISTRY.keys()),
                },
                "body":            {"type": "object", "description": "Request body (no secrets here)"},
            },
            "required": ["service", "endpoint", "method", "credential_name"],
        },
    },
]


SECRET_PATTERNS = [
    r"sk_[a-zA-Z0-9]{20,}",     # Stripe
    r"SG\.[a-zA-Z0-9]{30,}",    # SendGrid
    r"ghp_[a-zA-Z0-9]{36,}",    # GitHub PAT
    r"AKIA[A-Z0-9]{16}",         # AWS Access Key
    r"Bearer [a-zA-Z0-9\-._~+/]{20,}",  # Bearer tokens
    r"eyJ[a-zA-Z0-9\-_]{50,}",  # JWTs
]


def contains_secret(value: str) -> bool:
    """Detect if a string looks like a secret."""
    return any(re.search(p, value) for p in SECRET_PATTERNS)


def sanitize_args(args: dict) -> tuple[dict, list[str]]:
    """Recursively scan args for secrets and replace with [REDACTED]."""
    leaks = []
    clean = {}
    for k, v in args.items():
        if isinstance(v, str) and contains_secret(v):
            clean[k] = "[REDACTED — do not pass raw credentials as arguments]"
            leaks.append(k)
        elif isinstance(v, dict):
            clean[k], sub_leaks = sanitize_args(v)
            leaks.extend(f"{k}.{l}" for l in sub_leaks)
        else:
            clean[k] = v
    return clean, leaks


def execute_api_call(args: dict) -> dict:
    cred_name = args.get("credential_name")
    actual_key = CREDENTIAL_REGISTRY.get(cred_name)

    if not actual_key:
        return {"error": f"Unknown credential: {cred_name}. Available: {list(CREDENTIAL_REGISTRY.keys())}"}

    # actual_key is used here but never returned or logged
    service = args["service"]
    endpoint = args["endpoint"]
    method = args["method"]

    # Simulate API call
    return {
        "status": 200,
        "service": service,
        "endpoint": endpoint,
        "method": method,
        "credential_used": cred_name,  # name only, not the key
        "result": {"balance": {"usd": 10000}},
    }


def run_reference_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    # Pre-scan user message for secrets
    if contains_secret(user_message):
        print("  ⚠️ Secret detected in user message — do not echo back")

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=(
                "You manage API calls using named credentials from the registry. "
                "Never ask for or accept raw API keys. Use credential names like 'stripe_prod'. "
                f"Available credentials: {list(CREDENTIAL_REGISTRY.keys())}"
            ),
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                # Sanitize args before execution
                clean_args, leaks = sanitize_args(block.input)
                if leaks:
                    print(f"  🔴 Secret leak prevented in args: {leaks}")
                    block.input.update(clean_args)

                result = execute_api_call(block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(run_reference_agent("Check my Stripe production balance using the appropriate credential."))
```

**Expected Token Savings**: Zero secret bytes in context. Reference system adds ~20 tokens vs raw key passthrough.
**Environment**: Credential registry backed by environment variables. Swap for HashiCorp Vault or AWS Secrets Manager in production.

---

### Option 3: Output Scanner — Redact Secrets Before Returning to User

Scan every response and tool result for leaked secrets. Replace with redacted placeholders before the LLM or user sees them.

```python
import re
import json
import anthropic

client = anthropic.Anthropic()

# Comprehensive secret patterns
SECRET_PATTERNS = {
    "stripe_secret": r"sk_(?:live|test)_[a-zA-Z0-9]{24,}",
    "stripe_public": r"pk_(?:live|test)_[a-zA-Z0-9]{24,}",
    "github_pat":    r"ghp_[a-zA-Z0-9]{36,}",
    "github_oauth":  r"gho_[a-zA-Z0-9]{36,}",
    "sendgrid":      r"SG\.[a-zA-Z0-9\-_]{22}\.[a-zA-Z0-9\-_]{43}",
    "aws_key":       r"AKIA[A-Z0-9]{16}",
    "aws_secret":    r"[A-Za-z0-9/\+]{40}",  # broad, may have false positives
    "jwt":           r"eyJ[a-zA-Z0-9\-_]{20,}\.[a-zA-Z0-9\-_]{20,}\.[a-zA-Z0-9\-_]{20,}",
    "bearer_token":  r"Bearer [a-zA-Z0-9\-._~+/=]{20,}",
    "generic_key":   r"(?:api[_-]?key|secret|password|token|credential)['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9\-_]{20,})",
}


def redact_secrets(text: str, replacement: str = "[REDACTED]") -> tuple[str, dict[str, int]]:
    """
    Scan text for secrets and replace with redaction marker.
    Returns (cleaned_text, {pattern_name: count_found}).
    """
    found = {}
    result = text
    for name, pattern in SECRET_PATTERNS.items():
        matches = re.findall(pattern, result)
        if matches:
            found[name] = len(matches)
            result = re.sub(pattern, replacement, result)
    return result, found


def redact_dict(data: dict) -> tuple[dict, dict[str, int]]:
    """Recursively redact secrets in a dict."""
    all_found = {}
    cleaned = {}
    for k, v in data.items():
        if isinstance(v, str):
            cleaned_v, found = redact_secrets(v)
            cleaned[k] = cleaned_v
            for name, count in found.items():
                all_found[name] = all_found.get(name, 0) + count
        elif isinstance(v, dict):
            cleaned[k], sub_found = redact_dict(v)
            for name, count in sub_found.items():
                all_found[name] = all_found.get(name, 0) + count
        else:
            cleaned[k] = v
    return cleaned, all_found


TOOLS = [
    {
        "name": "call_external_api",
        "description": "Call an external API endpoint.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url":     {"type": "string"},
                "headers": {"type": "object"},
                "body":    {"type": "object"},
            },
            "required": ["url"],
        },
    },
]


def redacting_execute(tool_name: str, raw_args: dict) -> tuple[dict, dict]:
    """Execute tool with secret redaction on both input and output."""
    # Redact input args
    clean_args, input_leaks = redact_dict(raw_args)
    if input_leaks:
        print(f"  🔴 Secrets redacted from tool INPUT: {input_leaks}")

    # Execute (using clean args — leaked values already removed)
    if tool_name == "call_external_api":
        raw_result = {
            "status": 200,
            "body": {"user": "alice", "token": "Bearer sk-live-XXXXXXXXXXXXXXXXXXXXXXXX"},
        }
    else:
        raw_result = {"status": "ok"}

    # Redact output
    clean_result, output_leaks = redact_dict(raw_result)
    if output_leaks:
        print(f"  🔴 Secrets redacted from tool OUTPUT: {output_leaks}")

    return clean_result, {**input_leaks, **output_leaks}


def run_redacting_agent(user_message: str) -> str:
    # Redact secrets from user message before it enters context
    clean_message, user_leaks = redact_secrets(user_message)
    if user_leaks:
        print(f"  ⚠️ Secrets redacted from user message: {user_leaks}")

    messages = [{"role": "user", "content": clean_message}]
    total_leaks = {}

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            reply = next(b.text for b in response.content if b.type == "text")
            # Redact LLM response too
            clean_reply, reply_leaks = redact_secrets(reply)
            if reply_leaks:
                print(f"  ⚠️ Secrets redacted from LLM response: {reply_leaks}")
            return clean_reply

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result, leaks = redacting_execute(block.name, block.input)
                total_leaks.update(leaks)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]

    if total_leaks:
        print(f"  📊 Total secrets intercepted this session: {total_leaks}")


print(run_redacting_agent("Call https://api.example.com with Authorization: Bearer sk-live-abc123xyz789"))
```

**Expected Token Savings**: Zero — purely defensive. Prevents security incidents that would cost far more to remediate than any token savings.
**Environment**: Pure Python regex. Update `SECRET_PATTERNS` with your service-specific patterns.

---

### Option 4: Vault-Backed Credential Manager with Time-Limited References

Issue short-lived token references. The agent uses the reference; the real credential is never in the prompt.

```python
import os
import json
import time
import secrets
import hashlib
import anthropic

client = anthropic.Anthropic()


class CredentialVault:
    """
    Issues time-limited, single-use credential references.
    The actual credential is never exposed to the LLM.
    """
    def __init__(self, ttl_seconds: int = 300):
        self._credentials: dict[str, str] = {}   # name → actual_value
        self._refs: dict[str, dict] = {}          # ref_token → {name, expires_at, uses_left}
        self.ttl = ttl_seconds

    def register(self, name: str, value: str):
        """Register a credential by name."""
        self._credentials[name] = value

    def issue_ref(self, name: str, max_uses: int = 5) -> str | None:
        """Issue a time-limited reference token for a named credential."""
        if name not in self._credentials:
            return None
        ref = "ref_" + secrets.token_hex(8)
        self._refs[ref] = {
            "name": name,
            "expires_at": time.time() + self.ttl,
            "uses_left": max_uses,
        }
        return ref

    def resolve(self, ref: str) -> str | None:
        """Resolve a reference to the actual credential value. Returns None if expired/invalid."""
        meta = self._refs.get(ref)
        if not meta:
            return None
        if time.time() > meta["expires_at"]:
            del self._refs[ref]
            return None
        if meta["uses_left"] <= 0:
            return None
        meta["uses_left"] -= 1
        return self._credentials.get(meta["name"])

    def list_available(self) -> list[str]:
        """List credential names available to the LLM (names only, never values)."""
        return list(self._credentials.keys())


vault = CredentialVault(ttl_seconds=300)
vault.register("stripe",   os.environ.get("STRIPE_API_KEY", "sk-test-placeholder"))
vault.register("github",   os.environ.get("GITHUB_TOKEN", "ghp_placeholder"))
vault.register("database", os.environ.get("DB_PASSWORD", "db_placeholder"))

TOOLS = [
    {
        "name": "use_credential",
        "description": "Use a registered credential by name. Returns a temporary reference token.",
        "input_schema": {
            "type": "object",
            "properties": {
                "credential_name": {
                    "type": "string",
                    "description": "Name of the credential to use",
                },
                "action": {
                    "type": "string",
                    "description": "What action to perform with this credential",
                },
            },
            "required": ["credential_name", "action"],
        },
    },
]


def execute_credentialed_action(cred_name: str, action: str) -> dict:
    """Execute an action using a credential, without exposing the credential."""
    ref = vault.issue_ref(cred_name)
    if not ref:
        return {"error": f"Credential '{cred_name}' not found. Available: {vault.list_available()}"}

    actual = vault.resolve(ref)
    if not actual:
        return {"error": "Credential reference expired or invalid"}

    # actual is used here for the real API call — never returned
    print(f"  [Vault] Resolved {cred_name} for action: {action} (key length: {len(actual)})")

    # Simulate action
    return {
        "action": action,
        "credential_used": cred_name,
        "status": "success",
        "result": f"Completed: {action}",
    }


def run_vault_agent(user_message: str) -> str:
    system = f"Available credentials (by name): {vault.list_available()}. Never ask for or display actual credential values."
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_credentialed_action(
                    block.input["credential_name"],
                    block.input["action"],
                )
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(run_vault_agent("List my GitHub repos and check my Stripe account."))
```

**Expected Token Savings**: Zero — pure security control. Time-limited references expire in 5 minutes; single-use references prevent replay.
**Environment**: In-memory vault. Replace with HashiCorp Vault API for production.

---

### Option 5: Conversation History Sanitizer

Before saving or replaying conversation history, strip all secrets from stored messages.

```python
import re
import json
import copy
import anthropic

client = anthropic.Anthropic()

SECRET_REGEXES = [
    (r"sk_(?:live|test)_[a-zA-Z0-9]{24,}",    "[STRIPE_KEY]"),
    (r"SG\.[a-zA-Z0-9\-_]{65}",               "[SENDGRID_KEY]"),
    (r"ghp_[a-zA-Z0-9]{36,}",                 "[GITHUB_PAT]"),
    (r"AKIA[A-Z0-9]{16}",                      "[AWS_ACCESS_KEY]"),
    (r"eyJ[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+", "[JWT]"),
    (r"(?<=password['\": ]{1,5})[a-zA-Z0-9!@#$%^&*]{8,}", "[PASSWORD]"),
    (r"(?<=token['\": ]{1,5})[a-zA-Z0-9\-_]{20,}", "[TOKEN]"),
]


def sanitize_string(text: str) -> str:
    for pattern, replacement in SECRET_REGEXES:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def sanitize_message(message: dict) -> dict:
    """Deep sanitize a message dict."""
    msg = copy.deepcopy(message)
    content = msg.get("content", "")

    if isinstance(content, str):
        msg["content"] = sanitize_string(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    block["text"] = sanitize_string(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    if isinstance(block.get("input"), dict):
                        block["input"] = {
                            k: sanitize_string(str(v)) if isinstance(v, str) else v
                            for k, v in block["input"].items()
                        }
                elif block.get("type") == "tool_result":
                    content_inner = block.get("content", "")
                    if isinstance(content_inner, str):
                        block["content"] = sanitize_string(content_inner)
    return msg


class SanitizedHistoryAgent:
    def __init__(self):
        self._raw_history: list[dict] = []    # For current session (unredacted for tool execution)
        self._safe_history: list[dict] = []   # Sanitized for storage/replay

    def add_message(self, message: dict):
        self._raw_history.append(message)
        self._safe_history.append(sanitize_message(message))

    def save_history(self, path: str):
        """Save only the sanitized history."""
        with open(path, "w") as f:
            json.dump(self._safe_history, f, indent=2)
        print(f"  [Saved sanitized history to {path}]")

    def chat(self, user_message: str) -> str:
        self.add_message({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=self._raw_history,  # Use raw for accurate context
        )

        reply = response.content[0].text
        self.add_message({"role": "assistant", "content": reply})
        return reply


agent = SanitizedHistoryAgent()
agent.chat("My API key is sk-live-XXXXXXXXXXXXXXXXXXXXXXXXXXXX. Can you help?")
agent.chat("Now use that key to check my balance.")
agent.save_history("safe_history.json")
print("[Secrets redacted in saved history]")
```

**Expected Token Savings**: Sanitized history can be safely stored, shared with support, and replayed in testing without credential exposure risk.
**Environment**: File-based history storage. Run sanitizer before any persistence layer (database, S3, etc.).

---

### Option 6: Zero-Trust Tool Execution — Tool Never Receives Secrets

Design the architecture so tools receive only non-secret parameters. Credentials are bound to tool functions at server init, not at call time.

```python
import os
import json
from functools import partial
from typing import Callable
import anthropic

client = anthropic.Anthropic()


def make_stripe_checker(api_key: str) -> Callable:
    """Close over the API key — tool function never exposes it."""
    def check_balance(currency: str = "usd") -> dict:
        # api_key is in closure — invisible to callers
        # import stripe; stripe.api_key = api_key; return stripe.Balance.retrieve()
        return {"available": [{"amount": 10000, "currency": currency}]}
    return check_balance


def make_github_lister(token: str) -> Callable:
    def list_repos(org: str = "") -> dict:
        # token is in closure
        # headers = {"Authorization": f"token {token}"}
        return {"repos": ["project-a", "project-b"], "org": org or "personal"}
    return list_repos


def make_emailer(api_key: str) -> Callable:
    def send(to: str, subject: str, body: str) -> dict:
        # api_key is in closure
        return {"sent": True, "to": to}
    return send


# Bind credentials at startup — never touched by the LLM
BOUND_TOOLS: dict[str, Callable] = {
    "check_stripe_balance": make_stripe_checker(os.environ.get("STRIPE_API_KEY", "sk-test-placeholder")),
    "list_github_repos":    make_github_lister(os.environ.get("GITHUB_TOKEN",   "ghp_placeholder")),
    "send_email":           make_emailer(os.environ.get("SENDGRID_KEY",         "SG.placeholder")),
}

# LLM sees only the tool signatures — no credentials
TOOL_SCHEMAS = [
    {
        "name": "check_stripe_balance",
        "description": "Check Stripe account balance.",
        "input_schema": {
            "type": "object",
            "properties": {"currency": {"type": "string", "default": "usd"}},
        },
    },
    {
        "name": "list_github_repos",
        "description": "List GitHub repositories.",
        "input_schema": {
            "type": "object",
            "properties": {"org": {"type": "string", "description": "Organization (optional)"}},
        },
    },
    {
        "name": "send_email",
        "description": "Send an email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
                "body":    {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]


def run_zero_trust_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = BOUND_TOOLS.get(block.name)
                if fn:
                    result = fn(**block.input)
                else:
                    result = {"error": f"Unknown tool: {block.name}"}
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]


print(run_zero_trust_agent("Check my Stripe balance and list my GitHub repos."))
```

**Expected Token Savings**: Zero credential bytes ever enter any context. The architecture makes secret exposure structurally impossible.
**Environment**: Python closures at startup. This is the recommended approach — credentials bound once at init, invisible to all runtime code paths.

---

| Option | Defense Layer | Secret Visibility to LLM | Complexity | Best For |
|--------|-------------|--------------------------|-----------|---------|
| 1 | Server-side injection | Never | Low | Simple single-service agents |
| 2 | Named references + scanner | Names only | Medium | Multi-credential agents |
| 3 | Output scanner + redaction | Caught & redacted | Low | Defense-in-depth layer |
| 4 | Time-limited vault refs | Never | Medium | High-security, audit trail needed |
| 5 | History sanitizer | Stripped on save | Low | Safe logging and replay |
| 6 | Zero-trust closure binding | Never (structural) | Low | Production best practice |
