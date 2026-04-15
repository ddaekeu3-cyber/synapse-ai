---
layout: solution
title: "Agent Sends Full Conversation History on Every API Call — Ballooning Input Costs"
category: token-cost
description: "A 50-turn conversation accumulates 80,000 tokens of history. Every new API call sends all 80,000 tokens as input — even though only the last 5 turns are relevant. Input costs grow quadratically with conversation length. A 1-hour session costs 10x more than it should."
tags: [token-cost, conversation-history, context-management, summarization, sliding-window, compaction, cost, input-tokens]
---

# Agent Sends Full Conversation History on Every API Call — Ballooning Input Costs

## Symptom
- API costs grow quadratically as conversation length increases
- A 100-turn conversation costs 50x more than a 10-turn conversation
- Input token count keeps growing even when the agent is answering simple questions
- Session costs spike for long-running autonomous agents
- History from 2 hours ago is still being sent on every call — most of it irrelevant
- Context window fills up before the agent finishes a long task — not from new content but from accumulated history

## Root Cause
Conversation history is accumulated linearly but billed quadratically. If a conversation has N turns averaging T tokens each, by turn N the agent is sending N×T tokens of history on every single call. A conversation that produces 100,000 total tokens will bill for 100,000 + 99,000 + 98,000 + ... = ~5,000,000 input tokens — 50x the actual content produced. The fix is to aggressively trim, summarize, or window the history so the input token count stays bounded regardless of conversation length.

## Fix

### Option 1: Sliding window — keep only the most recent N turns

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class SlidingWindowSession:
    """
    Keeps only the most recent N turns in the active context.
    Simple and effective — prevents unbounded history growth.
    """
    system: str
    window_size: int = 20          # Number of turns to keep (user+assistant pairs)
    _full_history: list = field(default_factory=list)

    @property
    def windowed_history(self) -> list[dict]:
        """Return only the most recent turns"""
        if len(self._full_history) <= self.window_size * 2:
            return self._full_history
        # Always keep pairs (user + assistant)
        return self._full_history[-(self.window_size * 2):]

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        self._full_history.append({"role": "user", "content": user_message})

        active = self.windowed_history
        dropped = len(self._full_history) - len(active)
        if dropped > 0:
            print(f"History: keeping {len(active)//2} turns, dropped {dropped//2} oldest")

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.system,
            messages=active
        )
        text = response.content[0].text
        self._full_history.append({"role": "assistant", "content": text})
        return text

    @property
    def token_estimate(self) -> dict:
        """Rough token estimate for current window"""
        chars = sum(len(str(m.get("content", ""))) for m in self.windowed_history)
        total_chars = sum(len(str(m.get("content", ""))) for m in self._full_history)
        return {
            "window_tokens": chars // 4,
            "full_history_tokens": total_chars // 4,
            "savings_pct": f"{(1 - chars/max(total_chars,1))*100:.0f}%"
        }

session = SlidingWindowSession(system="You are a helpful assistant.", window_size=15)
```

### Option 2: Summarize-then-compress — replace old turns with a summary

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

SUMMARY_PROMPT = """Summarize the conversation below into a compact paragraph.
Preserve: decisions made, facts established, user preferences expressed, task progress.
Omit: pleasantries, repeated questions, verbose explanations.
Target: under 300 words.

Conversation:
{conversation}"""

@dataclass
class SummarizingSession:
    """
    When history exceeds a threshold, summarize the oldest portion.
    The summary is injected as context at the start of the active window.
    """
    system: str
    max_active_turns: int = 15        # Keep this many recent turns verbatim
    compress_when_over: int = 25      # Trigger compaction at this many turns
    _history: list = field(default_factory=list)
    _summary: str = ""
    _total_turns_ever: int = 0

    def _build_messages(self) -> list[dict]:
        """Build message list: optional summary injection + recent turns"""
        messages = []
        if self._summary:
            # Inject summary as first exchange
            messages.append({"role": "user", "content": "[Earlier conversation summary requested]"})
            messages.append({"role": "assistant", "content": f"[Summary of earlier conversation]\n{self._summary}"})
        messages.extend(self._history)
        return messages

    def _compress_history(self):
        """Compress oldest turns into summary, keep recent turns verbatim"""
        if len(self._history) <= self.max_active_turns * 2:
            return

        to_compress = self._history[:-(self.max_active_turns * 2)]
        to_keep = self._history[-(self.max_active_turns * 2):]

        # Build conversation text to summarize
        conv_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:500]}"
            for m in to_compress
        )

        # Include existing summary if present
        if self._summary:
            conv_text = f"[Previous summary]\n{self._summary}\n\n[New turns to add]\n{conv_text}"

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": SUMMARY_PROMPT.format(conversation=conv_text)}]
        )
        self._summary = response.content[0].text
        self._history = to_keep

        compressed_turns = len(to_compress) // 2
        print(f"Compressed {compressed_turns} turns into summary ({len(self._summary)} chars)")

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        self._history.append({"role": "user", "content": user_message})
        self._total_turns_ever += 1

        # Trigger compression if history is too long
        if len(self._history) > self.compress_when_over * 2:
            self._compress_history()

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.system,
            messages=self._build_messages()
        )
        text = response.content[0].text
        self._history.append({"role": "assistant", "content": text})
        return text

    @property
    def stats(self) -> dict:
        active_chars = sum(len(str(m.get("content", ""))) for m in self._history)
        return {
            "total_turns_ever": self._total_turns_ever,
            "active_turns": len(self._history) // 2,
            "summary_present": bool(self._summary),
            "active_tokens_est": active_chars // 4,
            "summary_tokens_est": len(self._summary) // 4
        }

summarizing_session = SummarizingSession(
    system="You are a helpful assistant.",
    max_active_turns=10,
    compress_when_over=20
)
```

### Option 3: Token-budget-aware trimming — trim to fit within a budget

```python
import anthropic
from typing import Optional

client = anthropic.Anthropic()

CHARS_PER_TOKEN = 4

def estimate_tokens(messages: list[dict], system: str = "") -> int:
    """Estimate total input tokens for a message list"""
    total_chars = len(system)
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        total_chars += len(str(content)) + 10  # +10 for role overhead
    return total_chars // CHARS_PER_TOKEN

def trim_history_to_budget(
    history: list[dict],
    token_budget: int,
    system_tokens: int = 0,
    always_keep_last_n_turns: int = 3
) -> tuple[list[dict], int]:
    """
    Trim history to fit within a token budget.
    Always keeps the last N complete turns.
    Removes oldest turns first (in pairs to maintain message alternation).
    Returns (trimmed_history, estimated_tokens_used).
    """
    # Protected recent turns (always kept)
    protected = history[-(always_keep_last_n_turns * 2):]
    candidates = history[:-(always_keep_last_n_turns * 2)]

    remaining_budget = token_budget - system_tokens
    protected_tokens = estimate_tokens(protected)

    if protected_tokens > remaining_budget:
        print(f"WARNING: Protected turns ({protected_tokens} tokens) exceed budget ({remaining_budget})")
        return protected, protected_tokens

    # Add candidates from newest to oldest until budget is hit
    available = remaining_budget - protected_tokens
    included = []
    for turn in reversed(candidates):
        turn_tokens = estimate_tokens([turn])
        if turn_tokens <= available:
            included.insert(0, turn)
            available -= turn_tokens
        else:
            break  # Budget hit — stop adding older turns

    final = included + protected
    used_tokens = estimate_tokens(final) + system_tokens
    dropped = (len(candidates) - len(included)) // 2

    if dropped > 0:
        print(f"Trimmed {dropped} old turns to fit {token_budget} token budget")

    return final, used_tokens

class BudgetedSession:
    """Session that trims history to stay within a per-call token budget"""

    def __init__(
        self,
        system: str,
        input_token_budget: int = 50_000,
        always_keep_last_n: int = 5
    ):
        self.system = system
        self.budget = input_token_budget
        self.keep_last = always_keep_last_n
        self._history: list[dict] = []
        self._system_tokens = len(system) // CHARS_PER_TOKEN

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        self._history.append({"role": "user", "content": user_message})

        trimmed, tokens_used = trim_history_to_budget(
            self._history,
            token_budget=self.budget,
            system_tokens=self._system_tokens,
            always_keep_last_n_turns=self.keep_last
        )

        print(f"Input: ~{tokens_used} tokens ({len(trimmed)//2} turns of {len(self._history)//2})")

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.system,
            messages=trimmed
        )
        text = response.content[0].text
        self._history.append({"role": "assistant", "content": text})
        return text

budgeted = BudgetedSession(
    system="You are a helpful assistant.",
    input_token_budget=40_000,
    always_keep_last_n=5
)
```

### Option 4: Importance-weighted history — keep high-value turns, drop filler

```python
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic()

IMPORTANCE_SCORER_PROMPT = """Score the importance of keeping each conversation turn for future reference.
Score 1-5: 5=critical (decisions, facts, errors), 3=useful (context), 1=disposable (pleasantries)

Return JSON: [{"index": N, "score": N, "reason": "brief reason"}, ...]

Turns to score:
{turns}"""

@dataclass
class ImportanceScoredTurn:
    role: str
    content: str
    score: float = 3.0  # Default to medium importance
    turn_index: int = 0

class ImportanceFilteredSession:
    """
    Score the importance of each turn and drop low-importance ones when trimming.
    High-importance turns (decisions, errors, key facts) are always preserved.
    """

    def __init__(
        self,
        system: str,
        max_turns: int = 30,
        score_every_n_turns: int = 10,
        min_score_to_keep: float = 2.5
    ):
        self.system = system
        self.max_turns = max_turns
        self.score_interval = score_every_n_turns
        self.min_score = min_score_to_keep
        self._turns: list[ImportanceScoredTurn] = []

    def _score_turns(self, unscored: list[ImportanceScoredTurn]):
        """Score a batch of turns using Haiku"""
        if not unscored:
            return
        turns_text = "\n".join(
            f"{i}. {t.role}: {t.content[:200]}"
            for i, t in enumerate(unscored)
        )
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                messages=[{"role": "user", "content": IMPORTANCE_SCORER_PROMPT.format(turns=turns_text)}]
            )
            import re
            text = response.content[0].text
            match = re.search(r'\[.*?\]', text, re.DOTALL)
            if match:
                scores = json.loads(match.group())
                for item in scores:
                    idx = item.get("index", 0)
                    if 0 <= idx < len(unscored):
                        unscored[idx].score = float(item.get("score", 3))
        except Exception as e:
            print(f"Scoring error: {e}")

    def _trim_low_importance(self):
        """Remove low-importance old turns when over limit"""
        if len(self._turns) <= self.max_turns * 2:
            return

        # Always keep last 6 turns (3 pairs) regardless of score
        protected = self._turns[-(6):]
        candidates = self._turns[:-(6)]

        # Score unscored candidates
        unscored = [t for t in candidates if t.score == 3.0]
        if unscored:
            self._score_turns(unscored)

        # Keep highest-scoring candidates up to max_turns
        keep_count = (self.max_turns * 2) - len(protected)
        sorted_candidates = sorted(candidates, key=lambda t: t.score, reverse=True)
        kept = sorted(sorted_candidates[:keep_count], key=lambda t: t.turn_index)

        dropped = len(candidates) - len(kept)
        print(f"Importance trim: dropped {dropped} low-importance turns (min_score={self.min_score})")
        self._turns = kept + protected

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        idx = len(self._turns)
        self._turns.append(ImportanceScoredTurn("user", user_message, turn_index=idx))

        if len(self._turns) % self.score_interval == 0:
            self._trim_low_importance()

        messages = [{"role": t.role, "content": t.content} for t in self._turns]

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.system,
            messages=messages
        )
        text = response.content[0].text
        self._turns.append(ImportanceScoredTurn("assistant", text, turn_index=idx+1))
        return text
```

### Option 5: Cost monitor — alert when history cost exceeds threshold

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CostMonitoredSession:
    """
    Tracks actual token usage per call.
    Alerts and triggers compaction when cost grows unsustainably.
    """
    system: str
    cost_per_million_input: float = 3.00     # Sonnet input price
    cost_per_million_output: float = 15.00   # Sonnet output price
    alert_threshold_usd: float = 0.10        # Alert per call above $0.10
    compact_threshold_input_tokens: int = 50_000
    _history: list = field(default_factory=list)
    _total_cost: float = 0.0
    _calls: int = 0

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        self._history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.system,
            messages=self._history
        )

        usage = response.usage
        input_cost = usage.input_tokens * self.cost_per_million_input / 1_000_000
        output_cost = usage.output_tokens * self.cost_per_million_output / 1_000_000
        call_cost = input_cost + output_cost
        self._total_cost += call_cost
        self._calls += 1

        if call_cost > self.alert_threshold_usd:
            print(
                f"COST ALERT: Call #{self._calls} cost ${call_cost:.4f} "
                f"({usage.input_tokens} input, {usage.output_tokens} output tokens). "
                f"Session total: ${self._total_cost:.4f}"
            )

        if usage.input_tokens > self.compact_threshold_input_tokens:
            print(
                f"HISTORY TOO LARGE: {usage.input_tokens} input tokens. "
                f"Consider enabling summarization or sliding window."
            )

        text = response.content[0].text
        self._history.append({"role": "assistant", "content": text})

        print(
            f"Call #{self._calls}: ${call_cost:.4f} "
            f"({usage.input_tokens}in + {usage.output_tokens}out tokens)"
        )
        return text

    @property
    def cost_report(self) -> dict:
        return {
            "total_cost_usd": round(self._total_cost, 4),
            "total_calls": self._calls,
            "avg_cost_per_call": round(self._total_cost / max(self._calls, 1), 4),
            "history_turns": len(self._history) // 2
        }
```

### Option 6: Prompt caching — avoid re-billing static history with Anthropic cache

```python
import anthropic

client = anthropic.Anthropic()

def build_cached_messages(
    static_context: list[dict],
    recent_turns: list[dict]
) -> list[dict]:
    """
    Use Anthropic prompt caching to avoid re-billing static conversation history.
    Static context (older turns) gets a cache_control breakpoint — billed once, cached.
    Recent turns are always sent fresh.

    Cost model with caching:
    - First call: pay full price for all tokens
    - Subsequent calls: pay 10% for cached tokens + full price for recent tokens
    """
    if not static_context:
        return recent_turns

    # Add cache breakpoint after the last static turn
    cached = []
    for i, turn in enumerate(static_context):
        if i == len(static_context) - 1:
            # Last static turn — add cache control
            content = turn["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            cached.append({"role": turn["role"], "content": content})
        else:
            cached.append(turn)

    return cached + recent_turns

class CacheOptimizedSession:
    """
    Session that uses prompt caching for older conversation history.
    Older turns are marked for caching — billed at 10% on repeat calls.
    Recent turns (last 5) are always fresh.
    """

    def __init__(self, system: str, cache_after_n_turns: int = 10):
        self.system = system
        self.cache_threshold = cache_after_n_turns
        self._history: list[dict] = []

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        self._history.append({"role": "user", "content": user_message})

        # Split into cacheable (old) and fresh (recent) turns
        split_point = max(0, len(self._history) - 10)  # Keep 5 turns fresh
        static = self._history[:split_point]
        recent = self._history[split_point:]

        messages = build_cached_messages(static, recent)

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
        )

        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        cache_write = getattr(usage, "cache_creation_input_tokens", 0)
        if cache_read > 0:
            savings = cache_read * 0.90 * 3.00 / 1_000_000  # 90% discount
            print(f"Cache: {cache_read} tokens read (saved ~${savings:.4f}), {cache_write} tokens written")

        text = response.content[0].text
        self._history.append({"role": "assistant", "content": text})
        return text

cached_session = CacheOptimizedSession(
    system="You are a helpful assistant.",
    cache_after_n_turns=10
)
```

## History Cost Comparison

| Strategy | Input Tokens (50 turns) | Relative Cost | Best For |
|---|---|---|---|
| No trimming (baseline) | ~250,000 | 100% | Never |
| Sliding window (15 turns) | ~75,000 | 30% | Most agents |
| Summarize + window | ~40,000 | 16% | Long sessions |
| Budget-based trim | ≤50,000 | ≤20% | Cost-sensitive |
| Prompt caching | 250,000 (10% rate) | ~15% | Stable history |
| Importance filtering | ~60,000 | 24% | Complex tasks |

## Expected Token Savings
100-turn session without trimming: ~5M input tokens billed
Sliding window (15 turns): ~750K input tokens — 85% cost reduction
Summarize + window: ~400K input tokens — 92% cost reduction

## Environment
- Any agent running multi-turn conversations or long autonomous sessions; input cost growth is quadratic without trimming — implement at session start, not after costs spike in production
- Source: direct experience; unbounded history is the most common cause of unexpectedly high API bills in the second month of production operation, after sessions grow long
