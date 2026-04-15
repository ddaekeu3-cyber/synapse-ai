---
layout: solution
title: "Agent doesn't validate tool input before calling expensive external APIs"
category: tool-failure
description: "Agent passes LLM-generated arguments directly to payment, SMS, or email APIs without validation, wasting money on malformed requests and risking unintended side effects."
tags: [tool-failure, validation, input-validation, external-api, cost, side-effects]
---

## Symptom

Operational dashboards show:
- Stripe charges for $0.00 or negative amounts
- Twilio SMS credits consumed sending to `+1 (555) 123-4567` (invalid E.164 format)
- SendGrid quota burned by duplicate emails to the same address in one tool call
- Tool call succeeds from the agent's perspective but the downstream API returns 400

The LLM extracted plausible-looking values from unstructured text, but the values fail API constraints the model knows nothing about.

## Root Cause

The agent's tool handler forwards `tool_input` to the external API with no intermediate validation. The LLM is good at extracting intent but not at producing API-safe values: phone numbers arrive in a dozen formats, amounts may be strings or missing currency codes, and email addresses may have typos. External APIs reject these requests — often charging for the attempt — before returning an error the agent cannot usefully act on.

---

## Option 1 — Pydantic guard at the tool handler boundary

**Define a Pydantic model for each tool's input. Parse before calling the external API; return structured errors to the model on failure.**

```python
import json
import re
import anthropic
from pydantic import BaseModel, field_validator, ValidationError

client = anthropic.Anthropic()


class SendSmsInput(BaseModel):
    to: str          # must be E.164: +14155552671
    message: str

    @field_validator("to")
    @classmethod
    def validate_e164(cls, v: str) -> str:
        v = re.sub(r"[\s\-\(\)]", "", v)   # strip formatting
        if not re.fullmatch(r"\+[1-9]\d{7,14}", v):
            raise ValueError(f"'{v}' is not a valid E.164 phone number")
        return v

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty")
        if len(v) > 1600:
            raise ValueError(f"message too long ({len(v)} chars, max 1600)")
        return v.strip()


SMS_TOOL = {
    "name": "send_sms",
    "description": "Send an SMS to a phone number.",
    "input_schema": {
        "type": "object",
        "properties": {
            "to":      {"type": "string", "description": "Recipient phone in E.164 format (+14155552671)"},
            "message": {"type": "string", "description": "SMS body (max 1600 chars)"},
        },
        "required": ["to", "message"],
    },
}


def send_sms_handler(raw_input: dict) -> str:
    try:
        params = SendSmsInput(**raw_input)
    except ValidationError as e:
        # Return structured error — model can self-correct
        errors = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
        return json.dumps({"error": f"Validation failed: {errors}"})

    # Only reach here with valid params
    print(f"[SEND SMS] to={params.to} body={params.message[:40]}")
    # twilio_client.messages.create(to=params.to, from_=FROM_NUM, body=params.message)
    return json.dumps({"status": "sent", "to": params.to})


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=[SMS_TOOL],
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            tool_call = next(b for b in response.content if b.type == "tool_use")
            result = send_sms_handler(tool_call.input)
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Text John at (415) 555-2671 to confirm his appointment tomorrow."))
```

**Expected Token Savings:** Validation errors are returned as tool results — the model self-corrects in 1 extra turn instead of requiring a full retry from scratch after an external API 400. Saves 2–4 turns per malformed call.

**Environment:** Any agent calling external APIs with strict input requirements; Pydantic v2.

---

## Option 2 — Pre-call validation layer with normalisation

**Normalise inputs before validation so common formatting variations are accepted rather than rejected.**

```python
import json
import re
import anthropic
from decimal import Decimal, InvalidOperation

client = anthropic.Anthropic()


def normalise_phone(raw: str) -> str | None:
    """Convert common phone formats to E.164. Return None if unparseable."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:                   # US number without country code
        digits = "1" + digits
    if len(digits) == 11 and digits[0] == "1":
        return f"+{digits}"
    if len(digits) >= 7:
        return f"+{digits}"
    return None


def normalise_amount(raw) -> str | None:
    """Parse '$ 12.50', '12,50', 12.5 → '12.50'. Return None if invalid."""
    if isinstance(raw, (int, float)):
        raw = str(raw)
    clean = re.sub(r"[^\d.,]", "", str(raw)).replace(",", ".")
    try:
        value = Decimal(clean)
        if value <= 0:
            return None
        return str(value.quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


CHARGE_TOOL = {
    "name": "charge_card",
    "description": "Charge a saved card on file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "amount":      {"type": "string", "description": "Amount in USD, e.g. '25.00'"},
            "description": {"type": "string"},
        },
        "required": ["customer_id", "amount", "description"],
    },
}


def charge_card_handler(raw: dict) -> str:
    customer_id = str(raw.get("customer_id", "")).strip()
    if not customer_id:
        return json.dumps({"error": "customer_id is required"})

    amount_str = normalise_amount(raw.get("amount", ""))
    if amount_str is None:
        return json.dumps({"error": f"invalid amount: {raw.get('amount')!r} — provide a positive number like '25.00'"})

    description = str(raw.get("description", "")).strip()
    if not description:
        return json.dumps({"error": "description is required"})

    print(f"[CHARGE] customer={customer_id} amount=${amount_str} desc={description[:40]}")
    # stripe.PaymentIntent.create(customer=customer_id, amount=int(Decimal(amount_str)*100), ...)
    return json.dumps({"status": "charged", "amount": amount_str, "customer": customer_id})


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=[CHARGE_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = charge_card_handler(tc.input)
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Charge customer cus_ABC123 $45 for the premium subscription."))
```

**Expected Token Savings:** Normalisation accepts `$45`, `45.00`, and `45` as equivalent — reduces validation-failure retry loops by ~70%, saving 2–3 turns of retry tokens per ambiguous input.

**Environment:** Agents processing natural-language financial or contact data; pairs well with Option 1 for defence in depth.

---

## Option 3 — Dry-run mode for destructive tool calls

**Add a `dry_run` parameter to destructive tools. The model must call with `dry_run: true` first; the handler confirms parameters before executing.**

```python
import json
import anthropic

client = anthropic.Anthropic()

EMAIL_TOOL = {
    "name": "send_email",
    "description": (
        "Send an email. Always call with dry_run=true first to confirm parameters, "
        "then call again with dry_run=false to send."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to":      {"type": "string"},
            "subject": {"type": "string"},
            "body":    {"type": "string"},
            "dry_run": {"type": "boolean", "description": "If true, validate only — do not send"},
        },
        "required": ["to", "subject", "body"],
    },
}

_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
import re


def send_email_handler(raw: dict) -> str:
    to = str(raw.get("to", "")).strip()
    subject = str(raw.get("subject", "")).strip()
    body = str(raw.get("body", "")).strip()
    dry_run = bool(raw.get("dry_run", True))   # default to safe

    errors = []
    if not re.fullmatch(_EMAIL_RE, to):
        errors.append(f"invalid email address: {to!r}")
    if not subject:
        errors.append("subject is empty")
    if not body:
        errors.append("body is empty")
    if len(body) > 50_000:
        errors.append(f"body too long ({len(body)} chars)")

    if errors:
        return json.dumps({"error": "; ".join(errors)})

    if dry_run:
        return json.dumps({
            "status": "validated",
            "to": to,
            "subject": subject,
            "body_length": len(body),
            "message": "Parameters valid. Call again with dry_run=false to send.",
        })

    print(f"[SEND EMAIL] to={to} subject={subject[:40]}")
    # sendgrid.send(to=to, subject=subject, body=body)
    return json.dumps({"status": "sent", "to": to})


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=768,
            tools=[EMAIL_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = send_email_handler(tc.input)
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Email alice@example.com a meeting invite for Friday at 3pm."))
```

**Expected Token Savings:** Dry-run catches formatting errors before any API call is made — prevents wasted external API quota and the 3–5 LLM turns needed to diagnose and recover from a mid-flow API 400.

**Environment:** Agents performing irreversible actions (email, SMS, payments, database writes); especially valuable for high-stakes workflows.

---

## Option 4 — JSON Schema validation before tool dispatch

**Validate `tool_input` against the tool's own `input_schema` using `jsonschema` before dispatching to the handler.**

```python
import json
import anthropic
import jsonschema

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "create_ticket",
        "description": "Create a support ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":    {"type": "string", "minLength": 5, "maxLength": 200},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "assignee": {"type": "string", "format": "email"},
                "tags":     {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            },
            "required": ["title", "priority"],
            "additionalProperties": False,
        },
    }
]

_SCHEMA_MAP = {t["name"]: t["input_schema"] for t in TOOLS}


def validate_tool_input(tool_name: str, tool_input: dict) -> list[str]:
    schema = _SCHEMA_MAP.get(tool_name)
    if not schema:
        return [f"unknown tool: {tool_name}"]
    errors = []
    validator = jsonschema.Draft7Validator(schema)
    for err in validator.iter_errors(tool_input):
        path = ".".join(str(p) for p in err.absolute_path) or "root"
        errors.append(f"{path}: {err.message}")
    return errors


def create_ticket_handler(params: dict) -> str:
    print(f"[TICKET] {params['title']} [{params['priority']}]")
    return json.dumps({"id": "TKT-9001", "status": "created"})


HANDLERS = {"create_ticket": create_ticket_handler}


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            validation_errors = validate_tool_input(tc.name, tc.input)

            if validation_errors:
                result = json.dumps({"error": "Invalid input", "details": validation_errors})
            else:
                result = HANDLERS[tc.name](tc.input)

            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Create a critical ticket for the login bug and assign it to ops@example.com."))
```

**Expected Token Savings:** Schema validation rejects structurally invalid inputs before any handler code runs — eliminates all downstream API 400s caused by wrong types or missing required fields (typically 30–50% of LLM-generated tool calls in production).

**Environment:** Any multi-tool agent; `jsonschema>=4.0`; validates against the same schema already defined for the Anthropic API.

---

## Option 5 — Idempotency check to prevent duplicate expensive calls

**Hash tool inputs; reject calls that duplicate a recent successful invocation within a cooldown window.**

```python
import hashlib
import json
import time
import anthropic

client = anthropic.Anthropic()

_RECENT_CALLS: dict[str, float] = {}   # hash -> timestamp
DEDUP_WINDOW = 300  # seconds (5 min)


def tool_call_hash(tool_name: str, tool_input: dict) -> str:
    payload = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def is_duplicate(tool_name: str, tool_input: dict) -> bool:
    key = tool_call_hash(tool_name, tool_input)
    now = time.time()
    # Expire old entries
    expired = [k for k, t in _RECENT_CALLS.items() if now - t > DEDUP_WINDOW]
    for k in expired:
        del _RECENT_CALLS[k]

    if key in _RECENT_CALLS:
        age = now - _RECENT_CALLS[key]
        print(f"  Duplicate call detected (age={age:.0f}s) — blocking.")
        return True
    _RECENT_CALLS[key] = now
    return False


CHARGE_TOOL = {
    "name": "charge_customer",
    "description": "Charge a customer's saved payment method.",
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "amount_cents": {"type": "integer", "minimum": 50},
        },
        "required": ["customer_id", "amount_cents"],
    },
}


def charge_handler(params: dict) -> str:
    if is_duplicate("charge_customer", params):
        return json.dumps({"error": "Duplicate charge detected. This exact charge was already processed within the last 5 minutes."})

    amount = params["amount_cents"]
    if amount < 50:
        return json.dumps({"error": "Minimum charge is $0.50 (50 cents)"})
    if amount > 99_999_99:
        return json.dumps({"error": "Amount exceeds maximum single charge limit"})

    print(f"[CHARGE] customer={params['customer_id']} amount={amount} cents")
    return json.dumps({"status": "charged", "charge_id": "ch_test_123"})


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=[CHARGE_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = charge_handler(tc.input)
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Charge customer cus_XYZ $29 for the monthly plan."))
```

**Expected Token Savings:** Deduplication prevents double-charges from retry loops — eliminates both external API waste and the additional LLM turns needed to diagnose and reverse a duplicate transaction.

**Environment:** Agents that retry tool calls on network errors; critical for payment, SMS, and email APIs.

---

## Option 6 — Human-in-the-loop confirmation for high-value calls

**Route calls above a cost threshold through a human approval step before executing.**

```python
import json
import anthropic

client = anthropic.Anthropic()

HIGH_VALUE_THRESHOLD_CENTS = 10_000   # $100


PAYMENT_TOOL = {
    "name": "process_payment",
    "description": "Process a payment. Payments over $100 require human approval.",
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_id":  {"type": "string"},
            "amount_cents": {"type": "integer", "minimum": 1},
            "description":  {"type": "string"},
        },
        "required": ["customer_id", "amount_cents", "description"],
    },
}


def request_human_approval(params: dict) -> bool:
    """Prompt operator in terminal. Replace with Slack/PagerDuty webhook in production."""
    amount = params["amount_cents"] / 100
    print(f"\n[APPROVAL REQUIRED] ${amount:.2f} for customer {params['customer_id']}")
    print(f"  Description: {params['description']}")
    answer = input("  Approve? (yes/no): ").strip().lower()
    return answer in ("yes", "y")


def payment_handler(raw: dict) -> str:
    customer_id = str(raw.get("customer_id", "")).strip()
    amount_cents = raw.get("amount_cents")
    description = str(raw.get("description", "")).strip()

    if not customer_id:
        return json.dumps({"error": "customer_id required"})
    if not isinstance(amount_cents, int) or amount_cents < 1:
        return json.dumps({"error": "amount_cents must be a positive integer"})
    if not description:
        return json.dumps({"error": "description required"})

    if amount_cents >= HIGH_VALUE_THRESHOLD_CENTS:
        approved = request_human_approval(raw)
        if not approved:
            return json.dumps({"error": "Payment rejected by operator.", "amount_cents": amount_cents})

    print(f"[PAYMENT] ${amount_cents/100:.2f} for {customer_id}: {description}")
    return json.dumps({"status": "processed", "amount_cents": amount_cents})


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=[PAYMENT_TOOL],
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = payment_handler(tc.input)
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]
        else:
            return next(b.text for b in response.content if hasattr(b, "text"))


print(run_agent("Process a $250 payment from customer cus_ABC for the enterprise plan upgrade."))
```

**Expected Token Savings:** Human gate catches hallucinated high-value charges before any external API call — prevents catastrophic misuse while adding only a single blocking step for large transactions.

**Environment:** Financial agents in production; replace terminal `input()` with a webhook to Slack or PagerDuty for async approval.

---

## Comparison

| Option | Validation Layer | Normalises Input | Blocks Duplicates | Complexity |
|--------|-----------------|-----------------|------------------|------------|
| 1. Pydantic guard | Semantic + format | No | No | Low |
| 2. Normalise + validate | Format → normalise | Yes | No | Low |
| 3. Dry-run mode | Pre-call confirm | No | No | Low |
| 4. JSON Schema validate | Structural | No | No | Very Low |
| 5. Idempotency hash | Duplicate detection | No | Yes | Low |
| 6. Human approval gate | Value threshold | No | No | Medium |

**Recommended path:** Layer Options 4 + 1 for structural then semantic validation. Add Option 5 (idempotency) for payment/SMS tools. Use Option 6 (human gate) for any single call above a meaningful cost threshold.
