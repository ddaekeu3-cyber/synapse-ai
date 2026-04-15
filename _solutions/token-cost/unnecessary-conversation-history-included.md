---
layout: solution
title: "Unnecessary Conversation History Included in Every API Call — Wasted Tokens"
category: token-cost
description: "Agent includes the full conversation history in every API call, even when previous turns are irrelevant to the current task. 50 turns of context sent for a simple one-shot question."
tags: [token-cost, conversation-history, context-management, sliding-window, irrelevant]
---

# Unnecessary Conversation History Included in Every API Call — Wasted Tokens

## Symptom
- Simple question costs 30,000 tokens because of conversation history
- Multi-turn conversation gets progressively slower and more expensive
- Agent includes debugging conversation from 30 turns ago when answering a new question
- API cost grows with session length even when questions are independent
- Old tool results and failed attempts bloat every subsequent call

## Root Cause
Most agent frameworks append every turn to a running history and include the full history in every API call. This is correct for stateful conversations where context matters, but wasteful when:
- The current question is independent of history
- History contains failed attempts and dead ends
- Tool results from early turns are no longer relevant

## Fix

### Option 1: Sliding window — keep only recent turns

```python
def sliding_window_history(
    history: list,
    max_turns: int = 10,
    always_keep_system: bool = True
) -> list:
    """Keep only the N most recent turns"""
    if not history:
        return history

    # Separate system messages from conversation
    system_messages = [m for m in history if m.get("role") == "system"]
    conversation = [m for m in history if m.get("role") != "system"]

    # Keep only recent turns
    if len(conversation) > max_turns * 2:  # *2 because each turn = user + assistant
        trimmed = conversation[-(max_turns * 2):]
        print(f"History trimmed: {len(conversation)} → {len(trimmed)} messages")
    else:
        trimmed = conversation

    return (system_messages if always_keep_system else []) + trimmed
```

### Option 2: Topic-based history reset

```python
TOPIC_CHANGE_PHRASES = [
    "new question", "different topic", "change subject", "forget the previous",
    "start over", "let's talk about", "now i want to", "moving on"
]

def detect_topic_change(user_message: str) -> bool:
    """Detect when user is starting a fresh topic"""
    msg_lower = user_message.lower()
    return any(phrase in msg_lower for phrase in TOPIC_CHANGE_PHRASES)

async def manage_history(history: list, new_user_message: str) -> list:
    if detect_topic_change(new_user_message):
        print("Topic change detected — resetting conversation history")
        return []  # Fresh start for new topic

    # Keep only recent relevant history
    return sliding_window_history(history, max_turns=8)
```

### Option 3: Relevance-based history filtering

```python
async def filter_relevant_history(
    history: list,
    current_query: str,
    agent,
    max_kept: int = 5
) -> list:
    """Keep only history turns relevant to current query"""
    if len(history) <= max_kept * 2:
        return history  # Small enough — keep all

    # Ask model to identify relevant turns
    history_summary = "\n".join([
        f"Turn {i//2 + 1}: {m['content'][:100]}"
        for i, m in enumerate(history)
        if m['role'] == 'user'
    ])

    relevance_check = await agent.complete([{
        "role": "user",
        "content": f"""Current question: {current_query}

Previous conversation turns:
{history_summary}

Which turn numbers (if any) contain information relevant to the current question?
List only the relevant turn numbers, comma-separated. If none, say 'none'."""
    }])

    # Parse relevant turn numbers
    relevant_turns = set()
    for token in relevance_check.replace("none", "").split(","):
        try:
            relevant_turns.add(int(token.strip()) - 1)  # 0-indexed
        except ValueError:
            pass

    # Extract relevant turns + last 2 turns always
    user_messages = [m for m in history if m['role'] == 'user']
    filtered = []
    for i, (user_msg, asst_msg) in enumerate(zip(user_messages, history[1::2])):
        if i in relevant_turns or i >= len(user_messages) - 2:
            filtered.extend([user_msg, asst_msg])

    print(f"History filtered: {len(history)} → {len(filtered)} messages")
    return filtered
```

### Option 4: Separate short-term and long-term memory

```python
class TieredMemory:
    """Short-term: recent turns. Long-term: key facts extracted from old turns."""

    def __init__(self, short_term_turns: int = 5):
        self.short_term_turns = short_term_turns
        self.recent_history = []
        self.long_term_facts = []

    def add_turn(self, user: str, assistant: str):
        self.recent_history.extend([
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant}
        ])

        # When history gets too long, compress oldest turns
        if len(self.recent_history) > self.short_term_turns * 2 + 2:
            oldest_pair = self.recent_history[:2]
            self.recent_history = self.recent_history[2:]
            # Extract key fact from old turn (async in real impl)
            fact = self._extract_fact(oldest_pair)
            if fact:
                self.long_term_facts.append(fact)

    def _extract_fact(self, turn_pair: list) -> str | None:
        """Extract key information worth preserving from old turn"""
        content = turn_pair[-1].get("content", "")
        # Simple heuristic: keep if contains key decisions or data
        keywords = ["decided", "confirmed", "the answer is", "result:", "found:"]
        if any(k in content.lower() for k in keywords):
            return content[:200]
        return None

    def build_messages(self, new_message: str) -> list:
        messages = []
        if self.long_term_facts:
            facts_context = "Key facts from earlier in our session:\n" + \
                           "\n".join(f"- {f}" for f in self.long_term_facts)
            messages.append({"role": "user", "content": facts_context})
            messages.append({"role": "assistant", "content": "Understood."})
        messages.extend(self.recent_history)
        messages.append({"role": "user", "content": new_message})
        return messages
```

### Option 5: Token budget for history

```python
def trim_history_to_budget(
    history: list,
    token_budget: int = 20000,
    always_keep_last_n: int = 4  # Always keep last 2 user+assistant pairs
) -> list:
    """Trim history to fit within token budget"""
    def estimate_tokens(messages: list) -> int:
        return sum(len(str(m.get("content", ""))) // 4 for m in messages)

    if estimate_tokens(history) <= token_budget:
        return history

    # Always keep last N messages
    preserved_tail = history[-always_keep_last_n:]
    trimable = history[:-always_keep_last_n]

    # Remove oldest messages until within budget
    while trimable and estimate_tokens(trimable + preserved_tail) > token_budget:
        trimable = trimable[2:]  # Remove oldest user+assistant pair
        print(f"Trimmed: {len(trimable)} messages remaining in trimable")

    result = trimable + preserved_tail
    print(f"History: {len(history)} → {len(result)} messages, ~{estimate_tokens(result):,} tokens")
    return result
```

## When History Is Needed vs. Wasteful

| Scenario | Keep history? | How much |
|---|---|---|
| Multi-step task in progress | Yes | Full recent history |
| New independent question | No | Reset or topic-switch |
| Debugging iterative code | Yes | All relevant turns |
| General Q&A chatbot | Partial | Last 5-10 turns |
| Document analysis | Partial | Recent + pinned context |
| Batch processing | No | Fresh context per item |

## Token Cost of History Strategies

| Strategy | Tokens per turn (10-turn session) | Cost ratio |
|---|---|---|
| Full history always | Growing: 5K → 50K | 10× at end |
| Sliding window (5 turns) | Constant ~5K | 1× always |
| Topic-reset | Constant ~1K | 0.2× per topic |
| Relevance filter | ~2-3K | 0.5× |

## Expected Token Savings
10-turn session, full history: ~50,000 total tokens
10-turn session, sliding window (5 turns): ~25,000 total tokens (50% savings)
Independent questions with reset: ~5,000 total tokens (90% savings)

## Environment
- Multi-turn conversational agents; most impactful for long sessions and Q&A bots
- Source: direct measurement, context management best practices
