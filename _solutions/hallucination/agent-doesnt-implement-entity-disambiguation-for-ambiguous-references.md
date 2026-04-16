---
title: "Agent Doesn't Implement Entity Disambiguation for Ambiguous References"
description: "How to detect and resolve ambiguous entity references—names, IDs, acronyms—before acting on them, preventing the agent from operating on the wrong target."
categories: [hallucination]
difficulty: intermediate
---

When a user says "update John's record" or "restart the API service," the agent may silently pick the wrong John or the wrong service. Entity disambiguation detects ambiguity early and resolves it explicitly before any action is taken.

## Solution 1: Candidate List Resolution

Generate candidate entities from the mention, rank them by context, and confirm before acting.

```python
import asyncio
import json
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"

# Simulated entity store
ENTITY_DB = {
    "users": [
        {"id": "u1", "name": "John Smith", "email": "john.smith@corp.com", "role": "engineer"},
        {"id": "u2", "name": "John Doe", "email": "john.doe@corp.com", "role": "manager"},
        {"id": "u3", "name": "John Park", "email": "john.park@corp.com", "role": "designer"},
    ],
    "services": [
        {"id": "svc1", "name": "API Gateway", "env": "production"},
        {"id": "svc2", "name": "API Gateway", "env": "staging"},
        {"id": "svc3", "name": "Auth API", "env": "production"},
    ],
}


def find_candidates(entity_type: str, mention: str) -> list[dict]:
    """Return all entities whose name contains the mention (case-insensitive)."""
    records = ENTITY_DB.get(entity_type, [])
    mention_lower = mention.lower()
    return [r for r in records if mention_lower in r["name"].lower()]


async def disambiguate(entity_type: str, mention: str, context: str) -> dict | None:
    candidates = find_candidates(entity_type, mention)

    if not candidates:
        return None  # No match at all

    if len(candidates) == 1:
        return candidates[0]  # Unambiguous

    # Multiple candidates — use LLM to pick the most likely given context
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Context: {context}\n\n"
                    f"Mention: '{mention}'\n\n"
                    f"Candidates:\n{json.dumps(candidates, indent=2)}\n\n"
                    f"Which candidate is most likely intended? Reply with the 'id' field only, "
                    f"or 'AMBIGUOUS' if the context does not disambiguate."
                ),
            }
        ],
    )
    chosen_id = resp.content[0].text.strip().strip('"')

    if chosen_id == "AMBIGUOUS":
        return None

    return next((c for c in candidates if c["id"] == chosen_id), None)


async def main():
    context = "The user asked to update the record for John in the engineering team."
    result = await disambiguate("users", "John", context)
    if result:
        print(f"Resolved to: {result}")
    else:
        candidates = find_candidates("users", "John")
        print(f"Ambiguous. Please specify which John: {[c['name'] for c in candidates]}")


asyncio.run(main())
```

## Solution 2: Acronym and Alias Registry

Expand acronyms and aliases before processing, using a domain-specific registry.

```python
import asyncio
import re
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"

ACRONYM_REGISTRY: dict[str, list[str]] = {
    "API": ["Application Programming Interface", "Auth API", "Analytics Pipeline Interface"],
    "ML":  ["Machine Learning", "Model Library"],
    "DB":  ["Database", "Dashboard Builder"],
    "CI":  ["Continuous Integration", "Customer Intelligence"],
    "KV":  ["Key-Value store", "Knowledge Vault"],
}

# Exact aliases (one-to-one)
ALIAS_MAP: dict[str, str] = {
    "the api": "API Gateway",
    "the db": "Primary Database",
    "prod": "production environment",
    "stage": "staging environment",
}


def expand_aliases(text: str) -> str:
    lower = text.lower()
    for alias, expansion in ALIAS_MAP.items():
        if alias in lower:
            text = re.sub(re.escape(alias), expansion, text, flags=re.IGNORECASE)
    return text


async def expand_acronyms(text: str, context: str) -> str:
    found = {acr: expansions for acr, expansions in ACRONYM_REGISTRY.items()
             if re.search(rf"\b{re.escape(acr)}\b", text)}

    if not found:
        return text

    # For each ambiguous acronym, ask the model to pick the right expansion
    resolved = {}
    for acr, expansions in found.items():
        if len(expansions) == 1:
            resolved[acr] = expansions[0]
        else:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=50,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"In this context: '{context}'\n"
                            f"What does '{acr}' most likely mean?\n"
                            f"Options: {expansions}\n"
                            f"Reply with only the chosen option."
                        ),
                    }
                ],
            )
            resolved[acr] = resp.content[0].text.strip()

    # Replace acronyms in text
    for acr, expansion in resolved.items():
        text = re.sub(rf"\b{re.escape(acr)}\b", f"{acr} ({expansion})", text)

    return text


async def preprocess_query(query: str) -> str:
    query = expand_aliases(query)
    query = await expand_acronyms(query, context=query)
    return query


async def main():
    queries = [
        "Restart the API and check the DB logs.",
        "Run the CI pipeline for the ML model.",
        "Deploy the KV service to prod.",
    ]
    for q in queries:
        expanded = await preprocess_query(q)
        print(f"Original:  {q}")
        print(f"Expanded:  {expanded}\n")


asyncio.run(main())
```

## Solution 3: Pronoun and Reference Coreference Resolution

Resolve pronouns ("it", "they", "that service") to the most recently mentioned concrete entity.

```python
import asyncio
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"

PRONOUN_PATTERN = {"it", "they", "them", "that", "this", "those", "these", "the service", "the system"}


@dataclass
class ConversationTracker:
    entity_stack: list[str] = field(default_factory=list)  # Most recently mentioned entities

    def update(self, entities: list[str]):
        self.entity_stack = entities + self.entity_stack
        self.entity_stack = self.entity_stack[:10]  # Keep last 10

    @property
    def most_recent(self) -> str | None:
        return self.entity_stack[0] if self.entity_stack else None


async def extract_entities(text: str) -> list[str]:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Extract all named entities (services, people, systems, files) from this text. "
                    f"Return a JSON array of strings. Text: {text}"
                ),
            }
        ],
    )
    import json
    try:
        return json.loads(resp.content[0].text)
    except Exception:
        return []


async def resolve_references(text: str, tracker: ConversationTracker) -> str:
    lower = text.lower()
    needs_resolution = any(p in lower for p in PRONOUN_PATTERN)

    if not needs_resolution or not tracker.most_recent:
        return text

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    f"The user said: '{text}'\n"
                    f"Recently mentioned entities: {tracker.entity_stack[:5]}\n\n"
                    f"Rewrite the sentence replacing any pronouns or vague references "
                    f"('it', 'they', 'that', etc.) with the most likely specific entity. "
                    f"Return only the rewritten sentence."
                ),
            }
        ],
    )
    return resp.content[0].text.strip()


async def process_conversation(turns: list[str]) -> list[str]:
    tracker = ConversationTracker()
    resolved_turns = []

    for turn in turns:
        resolved = await resolve_references(turn, tracker)
        entities = await extract_entities(resolved)
        tracker.update(entities)
        resolved_turns.append(resolved)

    return resolved_turns


async def main():
    conversation = [
        "Deploy the Payment Service to production.",
        "Now restart it.",
        "Check its logs for errors.",
        "Also scale them up to 5 replicas.",
    ]

    resolved = await process_conversation(conversation)
    for orig, res in zip(conversation, resolved):
        print(f"Original:  {orig}")
        print(f"Resolved:  {res}\n")


asyncio.run(main())
```

## Solution 4: Structured Entity Slot Filling

Before executing any action, require the agent to fill structured entity slots and confirm them explicitly.

```python
import asyncio
import json
from dataclasses import dataclass
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-sonnet-4-6"


@dataclass
class EntitySlot:
    name: str
    description: str
    required: bool = True
    resolved_value: Any = None

    @property
    def is_filled(self) -> bool:
        return self.resolved_value is not None


INTENT_SLOTS = {
    "update_user": [
        EntitySlot("user_id", "The unique ID of the user to update"),
        EntitySlot("field", "The field to update (e.g., email, role, name)"),
        EntitySlot("new_value", "The new value for the field"),
    ],
    "restart_service": [
        EntitySlot("service_name", "The exact name of the service to restart"),
        EntitySlot("environment", "The environment: production, staging, or development"),
    ],
    "delete_record": [
        EntitySlot("table", "The database table name"),
        EntitySlot("record_id", "The unique identifier of the record"),
    ],
}


async def fill_slots(intent: str, user_query: str) -> tuple[list[EntitySlot], list[str]]:
    slots = INTENT_SLOTS.get(intent, [])
    if not slots:
        return [], []

    slot_descriptions = json.dumps([{"name": s.name, "description": s.description} for s in slots])

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    f"User request: '{user_query}'\n\n"
                    f"Required slots:\n{slot_descriptions}\n\n"
                    f"Extract values for each slot from the user request. "
                    f"If a slot's value cannot be determined, use null. "
                    f"Reply with JSON: {{slot_name: value_or_null}}"
                ),
            }
        ],
    )

    try:
        extracted = json.loads(resp.content[0].text)
    except Exception:
        extracted = {}

    missing = []
    for slot in slots:
        value = extracted.get(slot.name)
        if value is not None:
            slot.resolved_value = value
        elif slot.required:
            missing.append(slot.name)

    return slots, missing


async def main():
    queries = [
        ("update_user", "Update John's email to newemail@corp.com"),
        ("restart_service", "Restart the API"),  # Missing: which environment?
        ("delete_record", "Delete user u123 from the users table"),
    ]

    for intent, query in queries:
        slots, missing = await fill_slots(intent, query)
        print(f"\nIntent: {intent}")
        print(f"Query: {query}")
        for s in slots:
            print(f"  {s.name}: {s.resolved_value}")
        if missing:
            print(f"  [BLOCKED] Missing required slots: {missing}")
        else:
            print(f"  [READY] All slots filled — proceeding with action")


asyncio.run(main())
```

## Solution 5: Cross-Turn Entity Memory with Conflict Detection

Maintain an entity memory across turns and detect when the same mention is used inconsistently.

```python
import asyncio
import json
from dataclasses import dataclass, field
from collections import defaultdict
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


@dataclass
class EntityRecord:
    canonical_name: str
    mentions: list[str] = field(default_factory=list)
    attributes: dict = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


class EntityMemory:
    def __init__(self):
        self._entities: dict[str, EntityRecord] = {}

    def register(self, mention: str, canonical: str, attributes: dict = {}):
        if canonical not in self._entities:
            self._entities[canonical] = EntityRecord(canonical_name=canonical)

        record = self._entities[canonical]
        record.mentions.append(mention)

        # Detect conflicts in attributes
        for key, val in attributes.items():
            if key in record.attributes and record.attributes[key] != val:
                record.conflicts.append(
                    f"Attribute '{key}' was {record.attributes[key]!r}, now {val!r}"
                )
            record.attributes[key] = val

    def get(self, mention: str) -> EntityRecord | None:
        mention_lower = mention.lower()
        for record in self._entities.values():
            if any(mention_lower in m.lower() for m in record.mentions):
                return record
        return None

    def has_conflicts(self) -> list[tuple[str, list[str]]]:
        return [(name, r.conflicts) for name, r in self._entities.items() if r.conflicts]


memory = EntityMemory()


async def extract_entities_from_turn(turn: str) -> list[dict]:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Extract entities from: '{turn}'\n"
                    f"Return JSON array: [{{\"mention\": str, \"canonical\": str, \"attributes\": dict}}]"
                ),
            }
        ],
    )
    try:
        return json.loads(resp.content[0].text)
    except Exception:
        return []


async def process_turn(turn: str) -> list[str]:
    entities = await extract_entities_from_turn(turn)
    warnings = []
    for e in entities:
        memory.register(e.get("mention", ""), e.get("canonical", ""), e.get("attributes", {}))

    conflicts = memory.has_conflicts()
    for entity_name, conflict_list in conflicts:
        for c in conflict_list:
            warnings.append(f"[CONFLICT] {entity_name}: {c}")

    return warnings


async def main():
    turns = [
        "The payment service runs on port 8080.",
        "Restart the payment service on port 9090.",  # Conflict!
        "The user John is an engineer.",
        "Update John's role to manager.",             # Conflict!
    ]

    for turn in turns:
        warnings = await process_turn(turn)
        print(f"Turn: {turn}")
        for w in warnings:
            print(f"  {w}")
        if not warnings:
            print(f"  [OK]")


asyncio.run(main())
```

## Solution 6: Disambiguation Confirmation Dialog

When ambiguity is detected, generate a natural-language clarification question rather than proceeding silently.

```python
import asyncio
import json
from dataclasses import dataclass
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


@dataclass
class DisambiguationRequest:
    original_query: str
    ambiguous_mention: str
    candidates: list[dict]
    clarification_question: str


async def build_clarification(
    original_query: str,
    ambiguous_mention: str,
    candidates: list[dict],
) -> str:
    candidates_text = "\n".join(
        f"- {i+1}. {json.dumps(c)}" for i, c in enumerate(candidates)
    )
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=150,
        messages=[
            {
                "role": "user",
                "content": (
                    f"The user said: '{original_query}'\n"
                    f"The term '{ambiguous_mention}' matches multiple entities:\n{candidates_text}\n\n"
                    f"Write a brief, friendly clarification question (1 sentence) to ask the user "
                    f"which one they meant. Include the key distinguishing attribute of each option."
                ),
            }
        ],
    )
    return resp.content[0].text.strip()


async def check_and_disambiguate(
    query: str,
    entity_type: str,
    mention: str,
    lookup_fn,
) -> DisambiguationRequest | None:
    candidates = lookup_fn(mention)

    if len(candidates) <= 1:
        return None  # No ambiguity

    clarification = await build_clarification(query, mention, candidates)
    return DisambiguationRequest(
        original_query=query,
        ambiguous_mention=mention,
        candidates=candidates,
        clarification_question=clarification,
    )


# Simulated lookup
def lookup_users(mention: str) -> list[dict]:
    all_users = [
        {"id": "u1", "name": "John Smith", "team": "Engineering"},
        {"id": "u2", "name": "John Doe", "team": "Marketing"},
    ]
    return [u for u in all_users if mention.lower() in u["name"].lower()]


def lookup_services(mention: str) -> list[dict]:
    all_services = [
        {"id": "s1", "name": "Auth Service", "env": "production"},
        {"id": "s2", "name": "Auth Service", "env": "staging"},
    ]
    return [s for s in all_services if mention.lower() in s["name"].lower()]


async def main():
    scenarios = [
        ("Deactivate John's account", "users", "John", lookup_users),
        ("Restart Auth Service", "services", "Auth Service", lookup_services),
        ("Update Jane's email to jane@corp.com", "users", "Jane", lookup_users),  # Unambiguous (no match)
    ]

    for query, etype, mention, fn in scenarios:
        result = await check_and_disambiguate(query, etype, mention, fn)
        if result:
            print(f"[AMBIGUOUS] {query}")
            print(f"  → {result.clarification_question}")
        else:
            candidates = fn(mention)
            if candidates:
                print(f"[OK] {query} — resolved to {candidates[0]['name']}")
            else:
                print(f"[NOT FOUND] {query} — no entity matching '{mention}'")
        print()


asyncio.run(main())
```

## Comparison

| Solution | Detection method | LLM calls | Requires DB | Best for |
|---|---|---|---|---|
| **Candidate list resolution** | Name lookup + LLM rank | 1 per ambiguity | Yes | Structured entity stores |
| **Acronym/alias registry** | Pattern match | 1 per acronym | No | Domain-specific jargon |
| **Coreference resolution** | Pronoun detection | 1 per turn | No | Multi-turn conversations |
| **Slot filling** | Intent-based extraction | 1 per action | No | Action-oriented agents |
| **Cross-turn conflict detection** | Attribute comparison | 1 per turn | No | Long-running sessions |
| **Clarification dialog** | Lookup + LLM question | 1 per ambiguity | Yes | User-facing interfaces |

Start with **slot filling** (Solution 4) for action-oriented agents — it catches missing context before execution. Add **candidate list resolution** (Solution 1) when you have a structured entity database. Use **clarification dialog** (Solution 6) for user-facing agents where asking a question is preferable to guessing wrong.
