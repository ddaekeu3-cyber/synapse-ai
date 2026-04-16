---
title: "Agent Doesn't Implement Graceful Context Compression Before Limit"
description: "Six solutions for detecting and compressing conversation context before hitting the context window limit, preserving essential information while reducing token count."
difficulty: intermediate
category: reliability
tags: [context-window, compression, summarization, reliability, long-conversations, memory]
---

# Agent Doesn't Implement Graceful Context Compression Before Limit

When a conversation grows long enough to hit the context limit, agents either crash with a context length error or silently drop the oldest messages — losing critical information. Proactive context compression detects the approaching limit and intelligently compresses history before it becomes a problem.

## Solution 1: Token-Count-Triggered LLM Summarization

Count tokens continuously; when approaching the limit, summarize older messages and replace them with a compact summary.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class CompressionConfig:
    model_context_limit: int = 200_000      # Claude's context window
    compression_threshold: float = 0.75     # Compress when 75% full
    target_history_tokens: int = 20_000     # Target after compression
    keep_recent_turns: int = 6              # Always keep last N turns verbatim


def estimate_tokens(messages: list[dict]) -> int:
    """Rough estimate: 1 token ≈ 4 characters."""
    total = sum(
        len(m.get("content", "") if isinstance(m.get("content"), str)
            else "".join(b.get("text", "") for b in m.get("content", []) if isinstance(b, dict)))
        for m in messages
    )
    return total // 4


class SummarizingAgent:
    SUMMARIZE_SYSTEM = """Summarize the following conversation history concisely.
Preserve: key decisions made, important facts established, user preferences stated,
unresolved issues, and any commitments made. Omit: greetings, filler, and redundant exchanges.
Output a compact paragraph or bullet list."""

    def __init__(self, config: CompressionConfig | None = None):
        self.client = AsyncAnthropic()
        self.config = config or CompressionConfig()
        self.messages: list[dict] = []
        self.system_prompt = "You are a helpful assistant."
        self.compression_count = 0
        self._summary_prefix: str | None = None

    async def _compress(self):
        """Summarize older messages; keep recent turns verbatim."""
        keep_n = self.config.keep_recent_turns
        if len(self.messages) <= keep_n:
            return  # Not enough history to compress

        older = self.messages[:-keep_n]
        recent = self.messages[-keep_n:]

        # Build conversation text for summarization
        conv_text = "\n".join(
            f"{m['role'].upper()}: {m['content'] if isinstance(m['content'], str) else '[complex content]'}"
            for m in older
        )
        summary_resp = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=self.SUMMARIZE_SYSTEM,
            messages=[{"role": "user", "content": f"Conversation to summarize:\n\n{conv_text}"}],
        )
        summary = summary_resp.content[0].text

        # Replace older messages with summary sentinel
        self._summary_prefix = summary
        self.messages = recent
        self.compression_count += 1
        print(
            f"[COMPRESS] Compressed {len(older)} messages into summary "
            f"(compression #{self.compression_count})"
        )

    def _build_messages_with_summary(self) -> list[dict]:
        """Prepend summary as a system-level context if present."""
        if not self._summary_prefix:
            return list(self.messages)
        # Inject summary as the first user/assistant exchange
        summary_injection = [
            {
                "role": "user",
                "content": "[Earlier conversation summary provided by system]",
            },
            {
                "role": "assistant",
                "content": f"[Summary of earlier conversation]\n{self._summary_prefix}",
            },
        ]
        return summary_injection + list(self.messages)

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        self.messages.append({"role": "user", "content": message})

        # Check if we need to compress before sending
        full_messages = self._build_messages_with_summary()
        estimated = estimate_tokens(full_messages)
        threshold_tokens = int(self.config.model_context_limit * self.config.compression_threshold)
        if estimated > threshold_tokens:
            await self._compress()
            full_messages = self._build_messages_with_summary()

        response = await self.client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.system_prompt,
            messages=full_messages,
        )
        text = response.content[0].text
        self.messages.append({"role": "assistant", "content": text})
        return text

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self._build_messages_with_summary())


async def demo_summarizing():
    agent = SummarizingAgent(CompressionConfig(
        model_context_limit=10_000,
        compression_threshold=0.7,
        keep_recent_turns=4,
    ))
    # Simulate a long conversation
    for i in range(15):
        response = await agent.chat(f"Tell me about topic {i} in detail. Provide a thorough explanation.")
        print(f"Turn {i+1}: ~{agent.estimated_tokens} tokens, compressions={agent.compression_count}")
```

## Solution 2: Sliding Window with Pinned Critical Messages

Keep a sliding window of recent messages; pin certain messages (tool results, key decisions) so they survive compression.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class AnnotatedMessage:
    role: str
    content: str
    pinned: bool = False      # Pinned messages always stay
    importance: int = 1       # 1-5; used to decide eviction order
    token_estimate: int = 0   # Filled on creation

    def __post_init__(self):
        self.token_estimate = len(self.content) // 4


class SlidingWindowAgent:
    def __init__(
        self,
        max_tokens: int = 50_000,
        reserve_output_tokens: int = 4096,
    ):
        self.client = AsyncAnthropic()
        self.max_context_tokens = max_tokens - reserve_output_tokens
        self._messages: list[AnnotatedMessage] = []
        self._system = "You are a helpful assistant."

    def _current_tokens(self) -> int:
        return sum(m.token_estimate for m in self._messages)

    def _evict_until_fits(self):
        """Remove unpinned, lowest-importance messages until within limit."""
        while self._current_tokens() > self.max_context_tokens:
            # Find first non-pinned message (oldest first)
            for i, msg in enumerate(self._messages):
                if not msg.pinned:
                    evicted = self._messages.pop(i)
                    print(
                        f"[WINDOW] Evicted '{evicted.role}' msg "
                        f"(~{evicted.token_estimate} tokens, importance={evicted.importance})"
                    )
                    break
            else:
                # All messages are pinned — nothing left to evict
                print("[WINDOW] Warning: all messages pinned, cannot evict further")
                break

    def add_message(
        self, role: str, content: str, pinned: bool = False, importance: int = 1
    ) -> AnnotatedMessage:
        msg = AnnotatedMessage(role=role, content=content, pinned=pinned, importance=importance)
        self._messages.append(msg)
        self._evict_until_fits()
        return msg

    async def chat(
        self, message: str, pin: bool = False, importance: int = 1
    ) -> str:
        self.add_message("user", message, pinned=pin, importance=importance)

        api_messages = [
            {"role": m.role, "content": m.content} for m in self._messages
        ]
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=self._system,
            messages=api_messages,
        )
        text = response.content[0].text
        self.add_message("assistant", text, pinned=False, importance=importance)
        return text

    def pin_message(self, index: int):
        if 0 <= index < len(self._messages):
            self._messages[index].pinned = True

    def window_stats(self) -> dict:
        return {
            "message_count": len(self._messages),
            "token_estimate": self._current_tokens(),
            "pinned_count": sum(1 for m in self._messages if m.pinned),
            "max_tokens": self.max_context_tokens,
            "utilization_pct": round(self._current_tokens() / self.max_context_tokens * 100, 1),
        }


async def demo_sliding_window():
    agent = SlidingWindowAgent(max_tokens=5000)

    # Pin an important decision
    await agent.chat("What database should I use for this project?", pin=True, importance=5)
    # Regular conversation
    for i in range(10):
        await agent.chat(f"Follow-up question {i} about implementation details and specific steps.", importance=2)

    print(f"Window stats: {agent.window_stats()}")
```

## Solution 3: Hierarchical Compression with Rolling Summary Chain

Maintain a chain of summaries at different time scales: recent (verbatim) → medium (paragraph) → ancient (one sentence).

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class SummaryLayer:
    level: int          # 0=recent verbatim, 1=medium summary, 2=ancient one-liner
    content: str
    turn_range: tuple[int, int]  # (start_turn, end_turn) this covers
    token_estimate: int = 0

    def __post_init__(self):
        self.token_estimate = len(self.content) // 4


class HierarchicalCompressionAgent:
    """
    Three-tier memory:
    - Level 0 (recent): Last 8 turns verbatim
    - Level 1 (medium): Turns 9-40 compressed to paragraphs
    - Level 2 (ancient): Turns 41+ compressed to a single context sentence
    """

    MEDIUM_COMPRESS = "Summarize this conversation segment in 2-3 sentences, preserving key facts and decisions."
    ANCIENT_COMPRESS = "Compress this to a single sentence capturing only the most critical context."

    def __init__(self):
        self.client = AsyncAnthropic()
        self._recent: list[dict] = []         # Verbatim
        self._medium_summaries: list[SummaryLayer] = []
        self._ancient_summary: SummaryLayer | None = None
        self._turn_count = 0
        self._recent_limit = 8
        self._medium_batch_size = 8
        self._medium_limit = 4  # Keep max 4 medium summaries before collapsing to ancient

    async def _compress_to_medium(self, messages: list[dict], turn_range: tuple) -> SummaryLayer:
        text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
        resp = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self.MEDIUM_COMPRESS,
            messages=[{"role": "user", "content": text}],
        )
        return SummaryLayer(level=1, content=resp.content[0].text, turn_range=turn_range)

    async def _compress_medium_to_ancient(self):
        combined = "\n".join(f"[Turns {s.turn_range[0]}-{s.turn_range[1]}]: {s.content}"
                             for s in self._medium_summaries)
        resp = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=self.ANCIENT_COMPRESS,
            messages=[{"role": "user", "content": combined}],
        )
        oldest = self._medium_summaries[0].turn_range[0]
        newest = self._medium_summaries[-1].turn_range[1]
        self._ancient_summary = SummaryLayer(
            level=2, content=resp.content[0].text, turn_range=(oldest, newest)
        )
        self._medium_summaries = []
        print(f"[HIER] Collapsed {len(self._medium_summaries)} medium summaries into ancient")

    async def _maybe_compress(self):
        if len(self._recent) > self._recent_limit:
            # Move oldest batch from recent to medium
            to_compress = self._recent[:self._medium_batch_size]
            self._recent = self._recent[self._medium_batch_size:]
            start_turn = self._turn_count - len(self._recent) - len(to_compress)
            end_turn = start_turn + len(to_compress)
            summary = await self._compress_to_medium(to_compress, (start_turn, end_turn))
            self._medium_summaries.append(summary)
            print(f"[HIER] Compressed {len(to_compress)} turns to medium summary")

        if len(self._medium_summaries) >= self._medium_limit:
            await self._compress_medium_to_ancient()

    def _build_context(self) -> list[dict]:
        """Assemble full context: ancient → medium summaries → recent verbatim."""
        messages = []
        if self._ancient_summary:
            messages.append({
                "role": "user",
                "content": f"[Ancient context - turns {self._ancient_summary.turn_range}]",
            })
            messages.append({
                "role": "assistant",
                "content": self._ancient_summary.content,
            })
        for summary in self._medium_summaries:
            messages.append({
                "role": "user",
                "content": f"[Conversation summary - turns {summary.turn_range}]",
            })
            messages.append({"role": "assistant", "content": summary.content})
        messages.extend(self._recent)
        return messages

    async def chat(self, message: str) -> str:
        self._recent.append({"role": "user", "content": message})
        self._turn_count += 1
        await self._maybe_compress()

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=self._build_context(),
        )
        text = response.content[0].text
        self._recent.append({"role": "assistant", "content": text})
        return text

    def memory_stats(self) -> dict:
        return {
            "turn_count": self._turn_count,
            "recent_turns": len(self._recent) // 2,
            "medium_summaries": len(self._medium_summaries),
            "has_ancient": self._ancient_summary is not None,
        }
```

## Solution 4: Semantic Importance Scoring for Selective Retention

Score each message by semantic importance; retain high-importance messages even when compressing.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class ScoredMessage:
    role: str
    content: str
    importance: float = 0.5   # 0.0-1.0; higher = more important to keep
    turn: int = 0

    @property
    def token_estimate(self) -> int:
        return len(self.content) // 4


class SemanticCompressionAgent:
    SCORE_SYSTEM = """Score each message's importance for context retention (0.0-1.0).
High importance (0.8-1.0): Decisions made, commitments, specific data/numbers, error descriptions, user requirements.
Medium importance (0.4-0.7): Explanations, examples, clarifications.
Low importance (0.0-0.3): Greetings, acknowledgements, filler phrases, repetitions.
Respond with ONLY a JSON array of floats, one per message, in order."""

    def __init__(self, max_tokens: int = 30_000):
        self.client = AsyncAnthropic()
        self.max_tokens = max_tokens
        self._messages: list[ScoredMessage] = []
        self._turn = 0

    async def _score_importance(self, messages: list[ScoredMessage]) -> list[float]:
        """Use LLM to score message importance."""
        import json
        msgs_text = "\n".join(
            f"{i}. [{m.role}]: {m.content[:200]}"
            for i, m in enumerate(messages)
        )
        try:
            resp = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=self.SCORE_SYSTEM,
                messages=[{"role": "user", "content": msgs_text}],
            )
            scores = json.loads(resp.content[0].text.strip())
            return [max(0.0, min(1.0, float(s))) for s in scores]
        except Exception:
            return [0.5] * len(messages)

    async def _compress(self):
        """Score all messages and drop low-importance ones to fit budget."""
        if len(self._messages) < 4:
            return

        # Score all messages
        scores = await self._score_importance(self._messages)
        for msg, score in zip(self._messages, scores):
            msg.importance = score

        # Sort by importance, evict lowest until within budget
        current_tokens = sum(m.token_estimate for m in self._messages)
        target = int(self.max_tokens * 0.6)

        if current_tokens <= target:
            return

        # Sort by importance ascending (drop lowest first) but keep order for API
        eviction_candidates = sorted(
            enumerate(self._messages),
            key=lambda x: (x[1].importance, -x[1].turn)  # Low importance + old = evict first
        )
        evicted_indices = set()
        for idx, msg in eviction_candidates:
            if current_tokens <= target:
                break
            if msg.importance < 0.5:  # Only evict low-importance
                evicted_indices.add(idx)
                current_tokens -= msg.token_estimate

        before = len(self._messages)
        self._messages = [m for i, m in enumerate(self._messages) if i not in evicted_indices]
        print(f"[SEMANTIC] Evicted {before - len(self._messages)} low-importance messages")

    async def chat(self, message: str) -> str:
        self._turn += 1
        self._messages.append(ScoredMessage("user", message, turn=self._turn))

        total = sum(m.token_estimate for m in self._messages)
        if total > self.max_tokens * 0.8:
            await self._compress()

        api_messages = [{"role": m.role, "content": m.content} for m in self._messages]
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=api_messages,
        )
        text = response.content[0].text
        self._turn += 1
        self._messages.append(ScoredMessage("assistant", text, turn=self._turn))
        return text
```

## Solution 5: Extractive Key-Point Preservation

Before compressing, extract key facts into a structured memory; use this memory as a compact prefix.

```python
import asyncio
import json
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class StructuredMemory:
    decisions: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    commitments: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        parts = []
        if self.decisions:
            parts.append("Decisions made:\n" + "\n".join(f"- {d}" for d in self.decisions))
        if self.facts:
            parts.append("Key facts:\n" + "\n".join(f"- {f}" for f in self.facts))
        if self.user_preferences:
            parts.append("User preferences:\n" + "\n".join(f"- {p}" for p in self.user_preferences))
        if self.open_questions:
            parts.append("Open questions:\n" + "\n".join(f"- {q}" for q in self.open_questions))
        if self.commitments:
            parts.append("Commitments:\n" + "\n".join(f"- {c}" for c in self.commitments))
        return "\n\n".join(parts) if parts else "No key information captured yet."

    @property
    def token_estimate(self) -> int:
        return len(self.to_text()) // 4


class ExtractiveMemoryAgent:
    EXTRACT_SYSTEM = """Extract key information from this conversation.
Output JSON with these fields (omit empty ones):
{
  "decisions": ["decision 1", ...],
  "facts": ["fact 1", ...],
  "user_preferences": ["preference 1", ...],
  "open_questions": ["question 1", ...],
  "commitments": ["commitment 1", ...]
}
Be concise — one short phrase per item."""

    def __init__(self, max_history_turns: int = 10):
        self.client = AsyncAnthropic()
        self.max_turns = max_history_turns
        self._messages: list[dict] = []
        self._memory = StructuredMemory()
        self._compression_count = 0

    async def _extract_and_compress(self):
        """Extract key points from old history; replace with memory."""
        old_messages = self._messages[:-self.max_turns]
        self._messages = self._messages[-self.max_turns:]

        conv_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:300]}"
            for m in old_messages
        )
        try:
            resp = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=self.EXTRACT_SYSTEM,
                messages=[{"role": "user", "content": conv_text}],
            )
            extracted = json.loads(resp.content[0].text.strip())
            self._memory.decisions.extend(extracted.get("decisions", []))
            self._memory.facts.extend(extracted.get("facts", []))
            self._memory.user_preferences.extend(extracted.get("user_preferences", []))
            self._memory.open_questions.extend(extracted.get("open_questions", []))
            self._memory.commitments.extend(extracted.get("commitments", []))
            self._compression_count += 1
            print(f"[EXTRACT] Extracted {sum(len(v) for v in extracted.values())} items")
        except (json.JSONDecodeError, Exception) as e:
            print(f"[EXTRACT] Extraction failed: {e}")

    def _build_context(self) -> tuple[str, list[dict]]:
        """System with memory + recent messages."""
        memory_text = self._memory.to_text()
        system = (
            "You are a helpful assistant.\n\n"
            f"<conversation_memory>\n{memory_text}\n</conversation_memory>"
        )
        return system, list(self._messages)

    async def chat(self, message: str) -> str:
        self._messages.append({"role": "user", "content": message})

        if len(self._messages) > self.max_turns * 2:
            await self._extract_and_compress()

        system, messages = self._build_context()
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        text = response.content[0].text
        self._messages.append({"role": "assistant", "content": text})
        return text

    def memory_report(self) -> dict:
        return {
            "compression_count": self._compression_count,
            "memory_items": {
                "decisions": len(self._memory.decisions),
                "facts": len(self._memory.facts),
                "preferences": len(self._memory.user_preferences),
                "questions": len(self._memory.open_questions),
                "commitments": len(self._memory.commitments),
            },
            "memory_tokens": self._memory.token_estimate,
            "active_history_turns": len(self._messages) // 2,
        }
```

## Solution 6: Token-Budget Controller with Pre-Flight Check

Check token budget before every request; proactively trim or summarize if the next request would exceed the limit.

```python
import asyncio
from dataclasses import dataclass
from anthropic import AsyncAnthropic


@dataclass
class TokenBudget:
    context_limit: int = 200_000
    max_output_tokens: int = 4096
    system_token_estimate: int = 500
    compression_target_pct: float = 0.60  # Compress to 60% of limit

    @property
    def available_for_history(self) -> int:
        return self.context_limit - self.max_output_tokens - self.system_token_estimate

    @property
    def compression_target_tokens(self) -> int:
        return int(self.available_for_history * self.compression_target_pct)


class TokenBudgetController:
    COMPRESS_SYSTEM = "Summarize the key points from this conversation in 3-5 bullet points. Be concise."

    def __init__(self, budget: TokenBudget | None = None):
        self.client = AsyncAnthropic()
        self.budget = budget or TokenBudget(
            context_limit=50_000,  # Demo limit
            max_output_tokens=2048,
        )
        self._messages: list[dict] = []
        self._compressions = 0

    def _estimate_tokens(self) -> int:
        return sum(len(str(m.get("content", ""))) // 4 for m in self._messages)

    async def _compress_to_fit(self, incoming_tokens: int):
        """Compress history so that history + incoming fits within budget."""
        available = self.budget.available_for_history - incoming_tokens
        if available <= 0:
            available = self.budget.compression_target_tokens // 2

        # Keep as many recent messages as fit
        kept = []
        tokens_kept = 0
        for msg in reversed(self._messages):
            msg_tokens = len(str(msg.get("content", ""))) // 4
            if tokens_kept + msg_tokens <= available * 0.7:
                kept.insert(0, msg)
                tokens_kept += msg_tokens
            else:
                break

        to_summarize = self._messages[:len(self._messages) - len(kept)]
        if not to_summarize:
            return

        conv_text = "\n".join(
            f"{m['role']}: {str(m.get('content', ''))[:200]}"
            for m in to_summarize
        )
        resp = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=self.COMPRESS_SYSTEM,
            messages=[{"role": "user", "content": conv_text}],
        )
        summary = resp.content[0].text
        summary_msg = [
            {"role": "user", "content": "[Earlier conversation summary]"},
            {"role": "assistant", "content": summary},
        ]
        self._messages = summary_msg + kept
        self._compressions += 1
        print(
            f"[BUDGET] Compressed {len(to_summarize)} → summary "
            f"(compression #{self._compressions}, now ~{self._estimate_tokens()} tokens)"
        )

    async def chat(self, message: str, system: str = "You are a helpful assistant.") -> str:
        incoming_tokens = len(message) // 4 + len(system) // 4
        current_tokens = self._estimate_tokens()

        # Pre-flight check: would this request exceed the limit?
        if current_tokens + incoming_tokens > self.budget.available_for_history:
            print(f"[BUDGET] Pre-flight: {current_tokens}+{incoming_tokens} > {self.budget.available_for_history}, compressing")
            await self._compress_to_fit(incoming_tokens)

        self._messages.append({"role": "user", "content": message})
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=self.budget.max_output_tokens,
            system=system,
            messages=self._messages,
        )
        text = response.content[0].text
        self._messages.append({"role": "assistant", "content": text})
        return text

    def budget_report(self) -> dict:
        return {
            "current_tokens": self._estimate_tokens(),
            "budget_tokens": self.budget.available_for_history,
            "utilization_pct": round(self._estimate_tokens() / self.budget.available_for_history * 100, 1),
            "compressions": self._compressions,
        }


async def demo_budget_controller():
    agent = TokenBudgetController(TokenBudget(context_limit=8000, max_output_tokens=1024))
    for i in range(20):
        await agent.chat(
            f"Explain topic {i} in comprehensive detail with examples and edge cases. "
            "Include multiple code samples and discuss trade-offs at length."
        )
        report = agent.budget_report()
        if i % 5 == 0:
            print(f"Turn {i+1}: {report['current_tokens']} tokens ({report['utilization_pct']}%), compressions={report['compressions']}")
```

## Comparison Table

| Solution | Compression Strategy | Information Loss | Latency Added | Best For |
|---|---|---|---|---|
| LLM Summarization | Full-history LLM summary | Low (LLM preserves meaning) | High (extra LLM call) | General-purpose conversations |
| Sliding Window | Simple eviction | Medium (old turns lost) | Zero | Memory-constrained, latency-sensitive |
| Hierarchical Chain | Multi-level rolling summaries | Low (layered preservation) | Medium | Very long-running sessions |
| Semantic Scoring | Importance-ranked eviction | Low (keeps important msgs) | High (scoring call) | Information-dense technical conversations |
| Extractive Memory | Key-point extraction to struct | Very low (structured memory) | Medium | Task-oriented agents with clear facts |
| Token Budget Controller | Pre-flight summary-then-evict | Medium | Medium | Production with strict context budgets |

**Recommended**: Use **Token Budget Controller** (Solution 6) in production — it's explicit about limits and compresses proactively before errors occur. Add **Extractive Memory** (Solution 5) for task-oriented agents where facts, decisions, and commitments must survive compression. Use **Sliding Window** (Solution 2) when latency is critical and you need zero compression overhead.
