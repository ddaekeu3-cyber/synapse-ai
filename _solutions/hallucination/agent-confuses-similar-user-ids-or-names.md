---
layout: solution
title: "Agent Confuses Similar User IDs or Names — Data Cross-Contamination"
category: hallucination
description: "Agent handles multiple users in the same session or batch. Refers to user A's data when answering about user B. Confuses 'John Smith' with 'John Smithson'. Or mixes up user_id 1234 with user_id 12345."
tags: [hallucination, user-data, isolation, context, contamination, privacy, multi-tenant]
---

# Agent Confuses Similar User IDs or Names — Data Cross-Contamination

## Symptom
- Agent returns user A's account balance when asked about user B
- Agent confuses `user_id=12345` with `user_id=1234` — one-digit difference
- In a batch task processing multiple users, agent carries over data from one user to the next
- Agent refers to John Smith's order when answering about John Smithson's account
- Multi-turn conversation mixes data from different users discussed earlier in the session
- Agent applies the wrong user's discount code to a different user's order

## Root Cause
When multiple users or similar identifiers appear in the same context window, the model may associate attributes with the wrong entity. This is especially common when: names are similar, IDs differ by one digit, multiple users are discussed in sequence, or the context contains references to several users without clear delimiters. It's not strictly hallucination — the data is present, but attributed to the wrong entity.

## Fix

### Option 1: Strict context isolation per user — never mix in same context

```python
import anthropic

client = anthropic.Anthropic()

class IsolatedUserContext:
    """
    Enforce strict context isolation: one conversation per user.
    Never mix multiple users' data in the same context window.
    """

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.history: list[dict] = []
        self.system_prompt = (
            f"You are serving user ID: {user_id}\n"
            f"IMPORTANT: Only refer to data explicitly provided for user {user_id}.\n"
            f"Never infer or use data from other users or sessions.\n"
            f"If asked about another user, refuse: 'I can only assist with user {user_id}.'"
        )

    def add_user_data(self, key: str, value) -> None:
        """Add user-specific data as a system injection"""
        self.history.append({
            "role": "user",
            "content": f"[System: User {self.user_id} data] {key}: {value}"
        })
        self.history.append({
            "role": "assistant",
            "content": f"Noted. {key} for user {self.user_id}: {value}"
        })

    async def ask(self, question: str) -> str:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            system=self.system_prompt,
            messages=self.history + [{"role": "user", "content": question}],
            max_tokens=1024
        )
        return response.content[0].text

# Never share context between users:
user_a = IsolatedUserContext("user_12345")
user_b = IsolatedUserContext("user_12346")

# These operate completely independently — zero cross-contamination possible
await user_a.ask("What is my balance?")
await user_b.ask("What is my balance?")
```

### Option 2: Always prefix data with full entity ID

```python
def build_user_context_block(user_data: dict) -> str:
    """
    Format user data with explicit, unambiguous entity prefixing.
    Every fact is labeled with the exact user ID to prevent attribution errors.
    """
    user_id = user_data["id"]
    lines = [f"=== DATA FOR USER_ID={user_id} ({user_data.get('name', 'Unknown')}) ==="]

    for key, value in user_data.items():
        if key == "id":
            continue
        lines.append(f"USER_{user_id}.{key} = {value}")

    lines.append(f"=== END OF DATA FOR USER_ID={user_id} ===")
    return "\n".join(lines)

# Example output:
# === DATA FOR USER_ID=12345 (John Smith) ===
# USER_12345.balance = $450.00
# USER_12345.plan = premium
# USER_12345.last_order = ORD-789
# === END OF DATA FOR USER_ID=12345 ===

# When processing multiple users:
def build_multi_user_context(users: list[dict]) -> str:
    blocks = [build_user_context_block(u) for u in users]
    header = (
        f"Processing {len(users)} users. "
        f"Each data block is labeled with the user ID. "
        f"Always reference the explicit USER_ID when citing data.\n\n"
    )
    return header + "\n\n".join(blocks)
```

### Option 3: One-at-a-time batch processing with context reset

```python
async def process_users_sequentially(
    user_ids: list[str],
    task_template: str,
    agent,
    context_reset_between: bool = True
) -> dict[str, str]:
    """
    Process each user in a fresh context to prevent data bleed.
    Never accumulate multiple users in the same conversation.
    """
    results = {}

    for user_id in user_ids:
        # Fresh context for each user — zero history from previous users
        user_data = await fetch_user_data(user_id)

        # Task with user-specific context, no other users mentioned
        prompt = (
            f"User context:\n{build_user_context_block(user_data)}\n\n"
            f"Task: {task_template.format(user_id=user_id)}"
        )

        result = await agent.call(
            system=f"You are processing data for user {user_id} ONLY.",
            messages=[{"role": "user", "content": prompt}],
            # New session — no history from other users
        )

        results[user_id] = result
        print(f"Processed user {user_id}")

    return results

# WRONG — all users in same context, risk of bleed:
# combined_prompt = "\n".join([f"User {uid}: {data}" for uid, data in all_users])
# agent.call(messages=[{"role": "user", "content": combined_prompt}])

# RIGHT — fresh context per user:
results = await process_users_sequentially(user_ids, "Summarize account status for {user_id}")
```

### Option 4: Structured response with entity verification

```python
import json
from pydantic import BaseModel, validator

class UserSpecificResponse(BaseModel):
    user_id: str
    answer: str
    data_used: list[str]  # Explicitly list which data points were used

    @validator("user_id")
    def must_match_requested(cls, v, values):
        return v  # Validated at call site

async def get_verified_response(
    target_user_id: str,
    question: str,
    user_data: dict,
    agent
) -> str:
    """
    Get response and verify the agent answered about the correct user.
    """
    system = (
        f"You are answering about user {target_user_id}.\n"
        f"Always begin your response with: 'For user {target_user_id}: '\n"
        f"Return JSON with: user_id, answer, data_used"
    )

    raw = await agent.call(
        system=system,
        messages=[{
            "role": "user",
            "content": f"User data:\n{json.dumps(user_data)}\n\nQuestion: {question}"
        }]
    )

    try:
        result = json.loads(raw)
        response_user_id = result.get("user_id", "")

        # Verify the response is actually about the right user
        if response_user_id != target_user_id:
            raise ValueError(
                f"Response user_id mismatch: requested {target_user_id}, "
                f"got {response_user_id}"
            )

        return result["answer"]
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"Invalid response for user {target_user_id}: {e}")
```

### Option 5: Canary values to detect cross-contamination

```python
import uuid

def inject_canary_values(user_data: dict, user_id: str) -> dict:
    """
    Add unique canary values to user data.
    If another user's canary appears in the response, contamination is detected.
    """
    canary = f"CANARY_{user_id}_{uuid.uuid4().hex[:8]}"
    augmented = dict(user_data)
    augmented["_canary"] = canary
    return augmented, canary

def detect_canary_leak(response: str, own_canary: str, all_canaries: dict) -> list[str]:
    """
    Check if another user's canary value appears in this user's response.
    """
    leaks = []
    for user_id, canary in all_canaries.items():
        if canary in response and canary != own_canary:
            leaks.append(f"Data from user {user_id} leaked into response")
    return leaks

# In batch processing:
canaries = {}
for user in users:
    data, canary = inject_canary_values(user["data"], user["id"])
    canaries[user["id"]] = canary

for user in users:
    response = await process_user(user)

    leaks = detect_canary_leak(response, canaries[user["id"]], canaries)
    if leaks:
        print(f"DATA CONTAMINATION DETECTED for user {user['id']}:")
        for leak in leaks:
            print(f"  {leak}")
        # Alert, log, and reject the contaminated response
```

### Option 6: System prompt for strict entity attribution

```
System prompt:
"Data attribution rules (strictly enforced):

1. When processing data for a specific user, ONLY reference data from that user's
   explicit data block. Do not infer or use data from any other context.

2. Before using any data point, confirm it appears in the labeled section for
   the user you're addressing.

3. When IDs or names are similar (user_12345 vs user_1234), treat them as
   completely different entities — never substitute one for the other.

4. When asked about 'the user' in a multi-user context, always ask which user
   (by exact ID) before proceeding.

5. Never say 'based on what I know about this user' — only say 'based on the
   data provided for user {user_id}:'

6. If data for the requested user is not present, say:
   'No data was provided for user {user_id} in this context.'"
```

## Cross-Contamination Risk Factors

| Risk factor | Contamination likelihood | Mitigation |
|---|---|---|
| Similar names (John Smith / John Smithson) | High | Full ID prefix on all data |
| Sequential IDs (1234 / 12345) | High | Always use full ID, never truncate |
| Multiple users in same context | High | Isolated context per user |
| Batch processing with accumulated history | Critical | Reset context between users |
| Pronouns without antecedents | Medium | Require explicit ID references |
| Implicit "previous user" reference | Medium | Forbid implicit references in system prompt |

## Expected Token Savings
Data contamination incident + investigation + user notification: ~500,000 tokens (plus legal/compliance cost)
Isolated contexts per user: 0 contamination possible

## Environment
- Multi-user agents, batch processing pipelines, customer service agents handling multiple accounts
- Source: direct experience; user data cross-contamination is a GDPR/privacy violation risk and the hardest contamination bug to detect
