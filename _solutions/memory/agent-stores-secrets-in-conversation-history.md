---
layout: solution
title: "Agent Stores API Keys and Secrets in Conversation History"
category: memory
description: "User pastes an API key or password into the chat. Agent stores it in conversation history and resends it in every subsequent API call. Secret persists in logs, context dumps, and error reports."
tags: [secrets, api-key, conversation-history, security, redaction, pii]
---

# Agent Stores API Keys and Secrets in Conversation History

## Symptom
- User provides API key in conversation: agent stores it in message history
- Secret appears in every API call's input tokens (logged by monitoring tools)
- Error reports and debug dumps expose API keys
- Conversation history saved to file/database contains plaintext secrets
- Agent shares the secret when asked to "summarize our conversation"

## Root Cause
LLM conversation history is just a list of messages — no automatic PII/secret detection or redaction. Whatever the user sends becomes part of the context and is resent in every subsequent API call. Monitoring systems, log aggregators, and error trackers that capture API payloads will capture the secrets too.

## Fix

### Option 1: Detect and redact secrets before adding to history

```python
import re

SECRET_PATTERNS = [
    (r'sk-ant-[a-zA-Z0-9\-_]{20,}', 'ANTHROPIC_KEY'),
    (r'sk-[a-zA-Z0-9]{48}', 'OPENAI_KEY'),
    (r'ghp_[a-zA-Z0-9]{36}', 'GITHUB_TOKEN'),
    (r'xoxb-[0-9]+-[a-zA-Z0-9]+', 'SLACK_TOKEN'),
    (r'AIza[0-9A-Za-z\-_]{35}', 'GOOGLE_API_KEY'),
    (r'(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*\S+', 'CREDENTIAL'),
    (r'(?i)bearer\s+[a-zA-Z0-9\-_\.]+', 'BEARER_TOKEN'),
]

def redact_secrets(text: str) -> str:
    """Remove known secret patterns from text"""
    for pattern, label in SECRET_PATTERNS:
        text = re.sub(pattern, f'[REDACTED:{label}]', text)
    return text

def add_message_safely(history: list, role: str, content: str) -> list:
    """Add message to history with secrets redacted"""
    redacted = redact_secrets(content)
    if redacted != content:
        print(f"Warning: Secret detected and redacted from {role} message")
    history.append({"role": role, "content": redacted})
    return history
```

### Option 2: Intercept secrets and store out-of-band

```python
import re, uuid

class SecretVault:
    """Store secrets separately, inject by reference at call time"""
    def __init__(self):
        self._secrets = {}  # token -> secret_value

    def store(self, secret: str) -> str:
        """Store secret, return opaque token"""
        token = f"SECRET_{uuid.uuid4().hex[:8]}"
        self._secrets[token] = secret
        return token

    def resolve(self, text: str) -> str:
        """Replace tokens with actual secrets at call time"""
        for token, value in self._secrets.items():
            text = text.replace(token, value)
        return text

vault = SecretVault()

# User provides: "My API key is sk-ant-xxx..."
# Instead of storing the key:
token = vault.store("sk-ant-xxx...")
# History stores: "My API key is SECRET_a1b2c3d4"
history.append({"role": "user", "content": f"My API key is {token}"})

# At API call time, resolve tokens in the actual request
# But better: use the secret directly from environment, not from conversation
```

### Option 3: Intercept and redirect to environment

```python
async def handle_user_provides_secret(user_message: str, agent) -> str:
    """Detect when user is providing a secret and redirect to env var"""
    secret_indicators = ["api key", "api_key", "password", "token", "secret", "credential"]
    is_providing_secret = any(ind in user_message.lower() for ind in secret_indicators)

    if is_providing_secret:
        return await agent.complete(
            "The user appears to be providing a secret/credential. "
            "Respond by asking them to set it as an environment variable instead of sharing it in chat. "
            "Explain that sharing secrets in chat is a security risk."
        )

    return await agent.complete(user_message)
```

### Option 4: Sanitize history before logging or saving

```python
def sanitize_history_for_storage(history: list) -> list:
    """Remove secrets before persisting conversation history"""
    sanitized = []
    for message in history:
        sanitized_content = redact_secrets(str(message.get("content", "")))
        sanitized.append({**message, "content": sanitized_content})
    return sanitized

def save_conversation(history: list, path: str):
    """Always sanitize before saving"""
    import json
    safe_history = sanitize_history_for_storage(history)
    with open(path, "w") as f:
        json.dump(safe_history, f, indent=2)

def log_api_call(messages: list, response):
    """Sanitize before logging API calls"""
    safe_messages = sanitize_history_for_storage(messages)
    logger.info(f"API call: {safe_messages} -> {response.usage}")
```

### Option 5: System prompt to refuse secret handling

```
System prompt:
"Security policy:
- NEVER store, repeat, or acknowledge API keys, passwords, tokens, or credentials
- If a user shares a secret in the conversation, immediately respond:
  'Please don't share credentials here. Store them in environment variables or a secrets manager.
  I've noted that you have a credential for [service] but I won't store the value.'
- Do not include credentials in summaries, logs, or responses
- Do not ask users to provide credentials in the conversation"
```

### Option 6: Pre-flight secret scan before API call

```python
def assert_no_secrets_in_payload(messages: list):
    """Raise if secrets would be sent to external API"""
    for msg in messages:
        content = str(msg.get("content", ""))
        for pattern, label in SECRET_PATTERNS:
            if re.search(pattern, content):
                raise SecurityError(
                    f"Refusing to send API call: {label} pattern detected in {msg['role']} message. "
                    f"Redact secrets before calling the API."
                )

# Call before every API request
assert_no_secrets_in_payload(messages)
response = client.messages.create(messages=messages, ...)
```

## Secret Detection Coverage

| Secret type | Pattern example | Risk if leaked |
|---|---|---|
| Anthropic API key | `sk-ant-api03-...` | Full API access, billing |
| OpenAI API key | `sk-...` (48 chars) | Full API access |
| GitHub token | `ghp_...` | Repo access, code |
| Slack token | `xoxb-...` | Workspace access |
| AWS key | `AKIA...` | Cloud infrastructure |
| Password | `password=...` | Account access |
| JWT | `eyJ...` | Session access |

## Expected Token Savings
Not about token savings — preventing serious security incidents.

## Environment
- Any agent where users might provide credentials; especially chatbots and coding assistants
- Source: security best practices, direct experience with credential exposure incidents
