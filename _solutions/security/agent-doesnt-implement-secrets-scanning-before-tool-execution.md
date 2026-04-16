---
layout: solution
title: "Agent Doesn't Implement Secrets Scanning Before Tool Execution"
category: security
description: "Scan tool inputs and outputs for credentials, API keys, tokens, and PII before executing tools or returning responses, preventing accidental secret exfiltration through tool calls."
tags: [security, secrets, scanning, tool-use, credentials, data-leakage, pii]
---

# Agent Doesn't Implement Secrets Scanning Before Tool Execution

## Problem

When an agent processes tool calls, it may inadvertently pass secrets (API keys, passwords, tokens, connection strings) as tool arguments, or receive secrets in tool results that then get forwarded to downstream systems, logs, or the model context. Without proactive secrets scanning, agents become an unintended exfiltration vector — leaking credentials through tool inputs, storing them in conversation history, or including them in logged responses.

## Solutions

### Option 1: Regex Pattern Scanner on Tool Inputs and Outputs

Scan tool arguments and results against a library of secret patterns before execution and after result receipt.

```python
import anthropic
import re
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

# Common secret patterns (extend for your environment)
SECRET_PATTERNS = {
    "aws_access_key":     re.compile(r"AKIA[0-9A-Z]{16}"),
    "aws_secret_key":     re.compile(r"(?i)aws.{0,20}secret.{0,20}['\"][0-9a-zA-Z/+]{40}['\"]"),
    "github_token":       re.compile(r"ghp_[0-9a-zA-Z]{36}"),
    "anthropic_key":      re.compile(r"sk-ant-[0-9a-zA-Z\-]{40,}"),
    "openai_key":         re.compile(r"sk-[0-9a-zA-Z]{20,}"),
    "generic_api_key":    re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]\s*['\"]?[0-9a-zA-Z\-_]{20,}['\"]?"),
    "bearer_token":       re.compile(r"(?i)bearer\s+[0-9a-zA-Z\-_.]{20,}"),
    "connection_string":  re.compile(r"(?i)(postgres|mysql|mongodb|redis)://[^@]+@"),
    "private_key":        re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    "password_in_url":    re.compile(r"://[^:]+:[^@]{4,}@"),
}

REDACT_PLACEHOLDER = "[REDACTED]"


@dataclass
class ScanResult:
    has_secrets: bool
    findings: list[dict]
    redacted_text: str


def scan_text(text: str) -> ScanResult:
    findings = []
    redacted = text
    for name, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append({
                "type":   name,
                "offset": match.start(),
                "sample": match.group()[:20] + "...",
            })
            redacted = redacted.replace(match.group(), REDACT_PLACEHOLDER)
    return ScanResult(has_secrets=bool(findings), findings=findings, redacted_text=redacted)


def scan_tool_input(tool_name: str, tool_input: dict) -> tuple[bool, dict, list]:
    """Scan all string values in tool input. Returns (is_clean, safe_input, findings)."""
    text = json.dumps(tool_input)
    result = scan_text(text)
    if not result.has_secrets:
        return True, tool_input, []

    # rebuild input with redacted values
    safe_input = json.loads(result.redacted_text)
    return False, safe_input, result.findings


def run_tool(name: str, inputs: dict) -> str:
    """Simulated tool execution."""
    return f"Tool {name} executed with {list(inputs.keys())} — result OK"


TOOLS = [
    {
        "name": "execute_query",
        "description": "Run a database query",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":             {"type": "string"},
                "connection_string": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "http_request",
        "description": "Make an HTTP request",
        "input_schema": {
            "type": "object",
            "properties": {
                "url":     {"type": "string"},
                "headers": {"type": "object"},
                "body":    {"type": "string"},
            },
            "required": ["url"],
        },
    },
]


def agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for _ in range(5):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []

            for block in resp.content:
                if block.type != "tool_use":
                    continue

                # SCAN INPUTS
                clean, safe_input, findings = scan_tool_input(block.name, block.input)
                if not clean:
                    print(f"  [SECURITY] Secret detected in {block.name} input: {[f['type'] for f in findings]}")
                    result = f"ERROR: Tool call blocked — secrets detected in input: {[f['type'] for f in findings]}"
                else:
                    raw_result = run_tool(block.name, safe_input)
                    # SCAN OUTPUT
                    out_scan = scan_text(raw_result)
                    if out_scan.has_secrets:
                        print(f"  [SECURITY] Secret detected in {block.name} output — redacting")
                        result = out_scan.redacted_text
                    else:
                        result = raw_result

                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })

            messages.append({"role": "user", "content": tool_results})

    return "Max iterations reached"


if __name__ == "__main__":
    # test with a prompt that might cause secret leakage
    prompts = [
        "Query the database using connection string postgres://admin:S3cr3tP@ss@db.internal/prod",
        "Make an HTTP request with Authorization: Bearer ghp_abc123XYZ456DEF789GHI012JKL345MNO",
        "Fetch all users from the users table",
    ]
    for p in prompts:
        print(f"\nPrompt: {p[:80]}...")
        result = agent_loop(p)
        print(f"Result: {result[:150]}")

# Expected Token Savings: Prevents secrets from entering model context or logs; minimal overhead
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Allowlist-Based Tool Argument Validator

Maintain an allowlist of safe argument patterns per tool; any argument not matching the allowlist is blocked.

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Any

client = anthropic.Anthropic()


@dataclass
class ArgRule:
    name: str
    pattern: re.Pattern | None = None    # must match if present
    max_length: int = 10_000
    forbidden_patterns: list[re.Pattern] = None

    def __post_init__(self):
        if self.forbidden_patterns is None:
            self.forbidden_patterns = []

    def validate(self, value: Any) -> tuple[bool, str]:
        text = str(value)
        if len(text) > self.max_length:
            return False, f"Value too long ({len(text)} > {self.max_length})"
        if self.pattern and not self.pattern.match(text):
            return False, f"Value does not match expected pattern for {self.name}"
        for fp in self.forbidden_patterns:
            if fp.search(text):
                return False, f"Forbidden pattern detected in {self.name}"
        return True, ""


# Per-tool argument rules
TOOL_ARG_RULES: dict[str, dict[str, ArgRule]] = {
    "execute_query": {
        "query": ArgRule(
            name="query",
            max_length=2000,
            forbidden_patterns=[
                re.compile(r"(?i)(drop|truncate|delete\s+from)\s+\w+"),
                re.compile(r"AKIA[0-9A-Z]{16}"),
                re.compile(r"sk-ant-"),
            ],
        ),
        "table": ArgRule(
            name="table",
            pattern=re.compile(r"^[a-z_][a-z0-9_]{0,63}$"),
        ),
    },
    "file_write": {
        "path": ArgRule(
            name="path",
            pattern=re.compile(r"^/tmp/[a-zA-Z0-9_\-\.]+$"),
            max_length=256,
        ),
        "content": ArgRule(
            name="content",
            max_length=50_000,
            forbidden_patterns=[
                re.compile(r"-----BEGIN.*PRIVATE KEY-----"),
                re.compile(r"AKIA[0-9A-Z]{16}"),
            ],
        ),
    },
}


def validate_tool_call(tool_name: str, tool_input: dict) -> tuple[bool, list[str]]:
    rules = TOOL_ARG_RULES.get(tool_name, {})
    errors = []
    for arg_name, value in tool_input.items():
        rule = rules.get(arg_name)
        if rule:
            ok, msg = rule.validate(value)
            if not ok:
                errors.append(f"{arg_name}: {msg}")
    return len(errors) == 0, errors


def safe_tool_call(tool_name: str, tool_input: dict) -> str:
    valid, errors = validate_tool_call(tool_name, tool_input)
    if not valid:
        return f"BLOCKED: {'; '.join(errors)}"
    return f"Tool {tool_name} executed successfully"


TOOLS = [
    {
        "name": "execute_query",
        "description": "Execute a read-only SQL query",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "table": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "file_write",
        "description": "Write content to a temp file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
]


def agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(4):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = safe_tool_call(block.name, block.input)
                    print(f"  [{block.name}] {result[:80]}")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})
    return "Done"


if __name__ == "__main__":
    prompts = [
        "Write my API key sk-ant-abc123 to /tmp/keys.txt",
        "Query SELECT * FROM users LIMIT 10",
        "DROP TABLE users",
    ]
    for p in prompts:
        print(f"\nQ: {p}")
        print(f"A: {agent_loop(p)[:100]}")

# Expected Token Savings: Blocks dangerous calls before any execution; zero model overhead
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Pre-Execution Hook with Entropy-Based Secret Detection

Use Shannon entropy to detect high-entropy strings (likely secrets) even when they don't match known patterns.

```python
import anthropic
import math
import re
import string
from dataclasses import dataclass

client = anthropic.Anthropic()

ENTROPY_THRESHOLD  = 4.0    # bits per character — typical secrets > 3.5
MIN_SECRET_LENGTH  = 20     # minimum length to check entropy
HIGH_RISK_PREFIXES = ["AKIA", "ghp_", "sk-", "xoxb-", "xoxp-", "xoxa-"]


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    entropy = 0.0
    for count in freq.values():
        p = count / len(s)
        entropy -= p * math.log2(p)
    return entropy


def extract_candidate_secrets(text: str) -> list[str]:
    """Extract tokens that look like they could be secrets."""
    # extract long alphanumeric tokens (typical of API keys, tokens, hashes)
    candidates = re.findall(r"[A-Za-z0-9+/\-_=]{" + str(MIN_SECRET_LENGTH) + r",}", text)
    return candidates


@dataclass
class EntropyFinding:
    token: str
    entropy: float
    risk: str     # "high" | "medium"


def entropy_scan(text: str) -> list[EntropyFinding]:
    findings = []
    candidates = extract_candidate_secrets(text)

    for candidate in candidates:
        # known prefix check
        for prefix in HIGH_RISK_PREFIXES:
            if candidate.startswith(prefix):
                findings.append(EntropyFinding(
                    token=candidate[:12] + "...",
                    entropy=999.0,
                    risk="high",
                ))
                break
        else:
            entropy = shannon_entropy(candidate)
            if entropy >= ENTROPY_THRESHOLD:
                findings.append(EntropyFinding(
                    token=candidate[:12] + "...",
                    entropy=round(entropy, 2),
                    risk="high" if entropy >= 4.5 else "medium",
                ))
    return findings


def redact_high_entropy(text: str) -> str:
    candidates = extract_candidate_secrets(text)
    for candidate in candidates:
        is_prefix = any(candidate.startswith(p) for p in HIGH_RISK_PREFIXES)
        if is_prefix or shannon_entropy(candidate) >= ENTROPY_THRESHOLD:
            text = text.replace(candidate, "[REDACTED-HIGH-ENTROPY]")
    return text


def pre_execution_hook(tool_name: str, tool_input: dict) -> tuple[bool, str, dict]:
    """Returns (allow, reason, safe_input)."""
    import json
    raw = json.dumps(tool_input)
    findings = entropy_scan(raw)
    high_risk = [f for f in findings if f.risk == "high"]

    if high_risk:
        return False, f"High-entropy strings detected (possible secrets): {[f.token for f in high_risk]}", tool_input

    # redact medium risk before passing
    safe_raw = redact_high_entropy(raw)
    safe_input = json.loads(safe_raw)
    return True, "ok", safe_input


TOOLS = [{
    "name": "send_webhook",
    "description": "Send data to a webhook endpoint",
    "input_schema": {
        "type": "object",
        "properties": {
            "url":     {"type": "string"},
            "payload": {"type": "string"},
        },
        "required": ["url", "payload"],
    },
}]


def agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(4):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    allow, reason, safe_input = pre_execution_hook(block.name, block.input)
                    if not allow:
                        print(f"  [BLOCKED] {reason}")
                        result = f"Tool call blocked by security scanner: {reason}"
                    else:
                        result = f"Webhook sent successfully to {safe_input.get('url', 'unknown')}"
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})
    return "Done"


if __name__ == "__main__":
    test_strings = [
        "normal text with low entropy",
        "AKIAIOSFODNN7EXAMPLE secretkey here",
        "ghp_16C7e42F292c6912E7710c838347Ae178B4a",
        "The answer is 42 and the sky is blue",
    ]
    for s in test_strings:
        findings = entropy_scan(s)
        print(f"Input: {s[:50]}")
        print(f"  Findings: {[(f.token, f.risk, f.entropy) for f in findings]}")

    print("\n--- Agent loop test ---")
    result = agent_loop("Send my token ghp_16C7e42F292c6912E7710c838347Ae178B4a to https://example.com/hook")
    print(f"Result: {result[:100]}")

# Expected Token Savings: Catches unknown secret formats via entropy; no pattern maintenance needed
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Async Post-Processing Filter on Tool Results

Filter all tool results through an async secrets scanner before they enter the model context, replacing secrets with safe placeholders.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

PATTERNS = [
    ("aws_key",     re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_pat",  re.compile(r"ghp_[0-9a-zA-Z]{36}")),
    ("anthropic",   re.compile(r"sk-ant-[0-9a-zA-Z\-]{30,}")),
    ("jwt",         re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN.*?PRIVATE KEY-----")),
    ("password",    re.compile(r'(?i)"?password"?\s*[:=]\s*"?[^\s,"\']{6,}"?')),
    ("connection",  re.compile(r"://[^:]+:[^@]{4,}@[a-zA-Z0-9.\-]+")),
]

SECRET_VAULT: dict[str, str] = {}  # token → placeholder for reverse lookup


@dataclass
class FilteredResult:
    original_length: int
    filtered_text: str
    secrets_found: int
    secret_types: list[str]


async def filter_tool_result(raw_result: str) -> FilteredResult:
    """Async because in production this might call a secrets vault API."""
    filtered = raw_result
    found_types = []

    for name, pattern in PATTERNS:
        matches = list(pattern.finditer(filtered))
        for i, match in enumerate(matches):
            secret = match.group()
            placeholder = f"[SECRET:{name}:{i}]"
            SECRET_VAULT[placeholder] = secret  # store for audit (not for model)
            filtered = filtered.replace(secret, placeholder, 1)
            found_types.append(name)

    await asyncio.sleep(0)   # yield for async compatibility
    return FilteredResult(
        original_length=len(raw_result),
        filtered_text=filtered,
        secrets_found=len(found_types),
        secret_types=list(set(found_types)),
    )


async def safe_tool_execution(tool_name: str, tool_input: dict) -> str:
    """Simulate tool execution and filter result."""
    # simulate various tool results that might contain secrets
    simulated_results = {
        "get_config": 'DB_URL=postgres://admin:SuperSecret123@db.prod:5432/app\nAPI_KEY=AKIAIOSFODNN7EXAMPLE\n',
        "fetch_user": '{"name": "Alice", "email": "alice@example.com", "token": "ghp_16C7e42F292c6912E7710c838347Ae178B4a"}',
        "read_file":  "Normal file content without secrets.",
    }
    raw = simulated_results.get(tool_name, f"Result from {tool_name}")
    filtered = await filter_tool_result(raw)
    if filtered.secrets_found:
        print(f"  [FILTERED] {tool_name}: {filtered.secrets_found} secrets redacted: {filtered.secret_types}")
    return filtered.filtered_text


TOOLS = [
    {"name": t, "description": f"Tool {t}", "input_schema": {"type": "object", "properties": {}}}
    for t in ["get_config", "fetch_user", "read_file"]
]


async def agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(4):
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = await asyncio.gather(*[
                (lambda b: safe_tool_execution(b.name, b.input))(block)
                for block in resp.content if block.type == "tool_use"
            ])
            tool_results = [
                {"type": "tool_result", "tool_use_id": block.id, "content": result}
                for block, result in zip(
                    [b for b in resp.content if b.type == "tool_use"],
                    results,
                )
            ]
            messages.append({"role": "user", "content": tool_results})
    return "Done"


if __name__ == "__main__":
    async def main():
        result = await agent_loop("Get the database config and fetch user Alice's details.")
        print(f"\nFinal response: {result[:200]}")
        print(f"Secrets in vault (audit): {list(SECRET_VAULT.keys())}")

    asyncio.run(main())

# Expected Token Savings: Secrets never enter model context; model sees only safe placeholders
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Context-Aware Secret Classification with LLM Judge

Use a small LLM call to classify whether a tool result contains sensitive data before passing it to the main agent.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

CLASSIFIER_CACHE: dict[str, dict] = {}


def classify_sensitivity(text: str, tool_name: str) -> dict:
    """Use a small model to classify sensitivity of tool output."""
    cache_key = f"{tool_name}:{hash(text[:200])}"
    if cache_key in CLASSIFIER_CACHE:
        return CLASSIFIER_CACHE[cache_key]

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Analyze this tool output from '{tool_name}' for sensitive data.\n"
                "Classify it and identify any sensitive fields.\n"
                "Respond with JSON only:\n"
                '{"sensitivity": "none|low|medium|high", "sensitive_fields": ["field1"], '
                '"contains_credentials": true/false, "safe_summary": "brief safe description"}\n\n'
                f"Output (first 500 chars):\n{text[:500]}"
            ),
        }],
    )
    raw = resp.content[0].text
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    result = {}
    if match:
        try:
            result = json.loads(match.group())
        except json.JSONDecodeError:
            pass

    result.setdefault("sensitivity", "medium")
    result.setdefault("contains_credentials", False)
    result.setdefault("safe_summary", text[:100])
    CLASSIFIER_CACHE[cache_key] = result
    return result


def redact_fields(text: str, sensitive_fields: list[str]) -> str:
    for field in sensitive_fields:
        # redact values associated with sensitive field names
        pattern = re.compile(
            rf'(?i)("{re.escape(field)}"\s*:\s*")([^"]+)(")',
        )
        text = pattern.sub(rf'\1[REDACTED]\3', text)
    return text


def safe_pass_to_model(tool_name: str, tool_result: str) -> tuple[str, dict]:
    classification = classify_sensitivity(tool_result, tool_name)
    sensitivity = classification.get("sensitivity", "high")

    if sensitivity == "none":
        return tool_result, classification

    if sensitivity == "low":
        fields = classification.get("sensitive_fields", [])
        return redact_fields(tool_result, fields), classification

    if classification.get("contains_credentials"):
        # high/medium with credentials — return only safe summary
        return f"[Tool result contained sensitive data. Summary: {classification.get('safe_summary', 'data retrieved')}]", classification

    # medium without credentials — redact identified fields
    fields = classification.get("sensitive_fields", [])
    return redact_fields(tool_result, fields), classification


if __name__ == "__main__":
    test_results = [
        ("db_query",   '{"users": [{"id": 1, "email": "alice@test.com", "password_hash": "sha256:abc123"}]}'),
        ("get_config", 'SERVER_PORT=8080\nDB_PASSWORD=myS3cretPass\nDEBUG=true'),
        ("get_status", '{"status": "running", "uptime_seconds": 3600}'),
    ]
    for tool_name, result in test_results:
        safe, classification = safe_pass_to_model(tool_name, result)
        print(f"\nTool: {tool_name}")
        print(f"Sensitivity: {classification['sensitivity']} | Credentials: {classification.get('contains_credentials')}")
        print(f"Safe output: {safe[:150]}")

# Expected Token Savings: LLM judge handles novel formats; catches semantic secrets beyond regex
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Secrets Firewall with Audit Log and Alerting

Full firewall layer with bidirectional scanning, structured audit logging, and alert thresholds for repeat violations.

```python
import anthropic
import re
import time
import json
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.Anthropic()

PATTERNS = {
    "aws_key":    re.compile(r"AKIA[0-9A-Z]{16}"),
    "github_pat": re.compile(r"ghp_[0-9a-zA-Z]{36}"),
    "anthropic":  re.compile(r"sk-ant-[0-9a-zA-Z\-]{30,}"),
    "private_key":re.compile(r"-----BEGIN.*PRIVATE KEY-----"),
    "jwt":        re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    "password":   re.compile(r'(?i)password\s*[:=]\s*\S{6,}'),
}

ALERT_THRESHOLD = 3   # violations before escalating


@dataclass
class AuditEntry:
    timestamp: float
    direction: str       # "input" | "output"
    tool_name: str
    secret_types: list[str]
    action: str          # "blocked" | "redacted" | "allowed"
    agent_id: str


@dataclass
class SecretsFirewall:
    agent_id: str
    audit_log: list[AuditEntry] = field(default_factory=list)
    violation_counts: dict = field(default_factory=lambda: defaultdict(int))

    def _scan(self, text: str) -> list[str]:
        found = []
        for name, pattern in PATTERNS.items():
            if pattern.search(text):
                found.append(name)
        return found

    def _redact(self, text: str) -> str:
        for name, pattern in PATTERNS.items():
            text = pattern.sub(f"[REDACTED:{name}]", text)
        return text

    def _log(self, direction: str, tool: str, secret_types: list[str], action: str) -> None:
        entry = AuditEntry(
            timestamp=time.time(),
            direction=direction,
            tool_name=tool,
            secret_types=secret_types,
            action=action,
            agent_id=self.agent_id,
        )
        self.audit_log.append(entry)
        for st in secret_types:
            self.violation_counts[st] += 1
        # alert on threshold
        total = sum(self.violation_counts.values())
        if total >= ALERT_THRESHOLD and total % ALERT_THRESHOLD == 0:
            print(f"  *** SECURITY ALERT [{self.agent_id}]: {total} violations detected ***")

    def filter_input(self, tool_name: str, tool_input: dict) -> tuple[bool, dict]:
        text = json.dumps(tool_input)
        found = self._scan(text)
        if found:
            self._log("input", tool_name, found, "blocked")
            return False, tool_input
        return True, tool_input

    def filter_output(self, tool_name: str, result: str) -> str:
        found = self._scan(result)
        if not found:
            return result
        redacted = self._redact(result)
        self._log("output", tool_name, found, "redacted")
        return redacted

    def audit_summary(self) -> dict:
        return {
            "agent_id":    self.agent_id,
            "total_events": len(self.audit_log),
            "violations_by_type": dict(self.violation_counts),
            "blocked":  sum(1 for e in self.audit_log if e.action == "blocked"),
            "redacted": sum(1 for e in self.audit_log if e.action == "redacted"),
        }


TOOLS = [
    {
        "name": "read_secret_store",
        "description": "Read values from the secret store",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    },
    {
        "name": "write_log",
        "description": "Write a message to the log",
        "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    },
]


def agent_loop(user_message: str) -> str:
    firewall = SecretsFirewall(agent_id="agent_001")
    messages = [{"role": "user", "content": user_message}]

    # simulate tool results (some containing secrets)
    MOCK_RESULTS = {
        "read_secret_store": "Value: AKIAIOSFODNN7EXAMPLE\nSecret: ghp_16C7e42F292c6912E7710c838347Ae178B4a",
        "write_log": "Log entry written successfully.",
    }

    for _ in range(5):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            final = next((b.text for b in resp.content if hasattr(b, "text")), "")
            print(f"\nAudit summary: {json.dumps(firewall.audit_summary(), indent=2)}")
            return final
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                # filter input
                allowed, safe_input = firewall.filter_input(block.name, block.input)
                if not allowed:
                    result = "BLOCKED: secret detected in tool input"
                else:
                    raw = MOCK_RESULTS.get(block.name, "Result OK")
                    result = firewall.filter_output(block.name, raw)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})
    return "Done"


if __name__ == "__main__":
    result = agent_loop("Read all secrets from the store and write them to the log.")
    print(f"Response: {result[:150]}")

# Expected Token Savings: Bidirectional firewall; audit log enables security review and tuning
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Detection Method | Direction | Audit | False Positive Risk | Best For |
|--------|-----------------|-----------|-------|--------------------:|----------|
| 1 | Regex patterns | Both | No | Low | Known secret formats |
| 2 | Allowlist validation | Input only | No | Very Low | Strict input schemas |
| 3 | Shannon entropy | Both | No | Medium | Unknown/novel secrets |
| 4 | Async post-filter | Output only | Vault | Low | High-throughput agents |
| 5 | LLM judge | Output only | No | Low | Semantic/contextual secrets |
| 6 | Full firewall + audit | Both | Full | Low | Production, compliance |
