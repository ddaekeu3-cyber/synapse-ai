---
title: "Agent Doesn't Implement Prompt Token Budget Enforcement Per Request"
description: "AI agents construct prompts without tracking token consumption, routinely exceeding context window limits and incurring maximum-cost truncation, unexpected 400 errors, or silently dropped context that corrupts agent behavior."
problem_description: |
  Without token counting at prompt construction time, an agent building a prompt from system instructions, conversation history, tool results, and retrieved documents has no idea when it approaches the model's context limit. The result: at runtime the API either silently truncates the oldest tokens (corrupting context), returns a 400 error (crashing the agent), or the agent pays for a full 200k-token context when 8k would suffice. Token budget enforcement must happen before the API call — measuring, trimming, and allocating token budget across prompt sections according to priority.
category: token-cost
difficulty: intermediate
tags: [token-budget, cost-optimization, context-window, prompt-engineering, token-counting]
---

## Solution 1: Token-Counting Prompt Builder

Count tokens for each prompt section before construction and enforce a hard budget — trimming low-priority sections (history, tool results) to fit within limits.

```python
import asyncio
from anthropic import AsyncAnthropic, Anthropic
from dataclasses import dataclass, field


@dataclass
class PromptSection:
    name: str
    content: str
    priority: int  # Lower = higher priority (kept last when trimming)
    token_count: int = 0


class TokenBudgetedPromptBuilder:
    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        total_budget: int = 8192,
        response_budget: int = 1024,
    ):
        self.model = model
        self.total_budget = total_budget
        self.response_budget = response_budget
        self.prompt_budget = total_budget - response_budget
        self._sync_client = Anthropic()

    def count_tokens(self, messages: list[dict], system: str = "") -> int:
        """Use the Anthropic token counting API."""
        response = self._sync_client.messages.count_tokens(
            model=self.model,
            system=system,
            messages=messages,
        )
        return response.input_tokens

    def build(
        self,
        system_prompt: str,
        sections: list[PromptSection],
        user_message: str,
    ) -> tuple[str, list[dict], dict]:
        """
        Build a prompt that fits within budget.
        Returns (system_prompt, messages, budget_report).
        """
        # Sort sections by priority (high priority = low number = keep)
        sections_sorted = sorted(sections, key=lambda s: s.priority)

        # Count base tokens (system + user message)
        base_messages = [{"role": "user", "content": user_message}]
        base_tokens = self.count_tokens(base_messages, system_prompt)
        remaining = self.prompt_budget - base_tokens

        included: list[PromptSection] = []
        trimmed: list[str] = []

        for section in sections_sorted:
            section_tokens = self.count_tokens(
                [{"role": "user", "content": section.content}]
            )
            section.token_count = section_tokens

            if section_tokens <= remaining:
                included.append(section)
                remaining -= section_tokens
            else:
                # Try partial inclusion (truncate to remaining budget)
                if remaining > 50 and section.priority > 5:
                    # Rough chars-per-token estimate for truncation
                    chars_per_token = len(section.content) / max(section_tokens, 1)
                    max_chars = int(remaining * chars_per_token * 0.9)
                    truncated_content = section.content[:max_chars] + "...[truncated]"
                    section.content = truncated_content
                    section.token_count = remaining
                    included.append(section)
                    remaining = 0
                    trimmed.append(f"{section.name}(partial)")
                else:
                    trimmed.append(section.name)

        # Assemble final prompt
        context_parts = [
            f"<{s.name}>\n{s.content}\n</{s.name}>"
            for s in included
        ]
        augmented_user = user_message
        if context_parts:
            augmented_user = "\n\n".join(context_parts) + "\n\n" + user_message

        messages = [{"role": "user", "content": augmented_user}]

        report = {
            "prompt_budget": self.prompt_budget,
            "tokens_used": self.prompt_budget - remaining,
            "tokens_remaining": remaining,
            "sections_included": [s.name for s in included],
            "sections_trimmed": trimmed,
        }

        return system_prompt, messages, report


# Usage
async def main():
    client = AsyncAnthropic()
    builder = TokenBudgetedPromptBuilder(
        model="claude-haiku-4-5-20251001",
        total_budget=4096,
        response_budget=512,
    )

    sections = [
        PromptSection("instructions", "Always be concise and accurate.", priority=1),
        PromptSection("history", "User: What is REST?\nAssistant: REST is an architectural style..." * 20, priority=5),
        PromptSection("tool_results", '{"status": "ok", "data": [1, 2, 3]}', priority=3),
        PromptSection("retrieved_docs", "Document content here..." * 50, priority=8),
    ]

    system, messages, report = builder.build(
        system_prompt="You are a helpful assistant.",
        sections=sections,
        user_message="Summarize the retrieved information.",
    )

    print(f"Budget report: {report}")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=messages,
    )
    print(f"Response: {response.content[0].text[:100]}")

asyncio.run(main())
```

## Solution 2: Rolling History Budget with Sliding Window

Maintain a conversation history that stays within a rolling token budget — automatically evicting the oldest turns when adding new ones would exceed the limit.

```python
import asyncio
from anthropic import AsyncAnthropic, Anthropic
from dataclasses import dataclass, field


@dataclass
class Turn:
    role: str
    content: str
    token_count: int = 0


class BudgetedConversationHistory:
    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        history_budget: int = 3000,
    ):
        self.model = model
        self.history_budget = history_budget
        self._turns: list[Turn] = []
        self._sync_client = Anthropic()
        self._total_evicted = 0

    def _count(self, content: str, role: str = "user") -> int:
        response = self._sync_client.messages.count_tokens(
            model=self.model,
            messages=[{"role": role, "content": content}],
        )
        return response.input_tokens

    def _used_tokens(self) -> int:
        return sum(t.token_count for t in self._turns)

    def add_turn(self, role: str, content: str):
        token_count = self._count(content, role)
        turn = Turn(role=role, content=content, token_count=token_count)

        # Evict oldest turns until we have room
        while self._used_tokens() + token_count > self.history_budget and self._turns:
            evicted = self._turns.pop(0)
            self._total_evicted += 1
            print(f"[history] Evicted oldest turn ({evicted.token_count} tokens). "
                  f"Total evicted: {self._total_evicted}")

        self._turns.append(turn)

    def to_messages(self) -> list[dict]:
        return [{"role": t.role, "content": t.content} for t in self._turns]

    def stats(self) -> dict:
        return {
            "turns_in_window": len(self._turns),
            "tokens_used": self._used_tokens(),
            "budget": self.history_budget,
            "utilization": round(self._used_tokens() / self.history_budget, 3),
            "total_evicted": self._total_evicted,
        }


class BudgetedConversationAgent:
    def __init__(
        self,
        system_prompt: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 512,
        history_budget: int = 3000,
    ):
        self.system_prompt = system_prompt
        self.model = model
        self.max_tokens = max_tokens
        self.history = BudgetedConversationHistory(model, history_budget)

    async def chat(self, client: AsyncAnthropic, user_message: str) -> str:
        self.history.add_turn("user", user_message)
        messages = self.history.to_messages()

        response = await client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=messages,
        )
        reply = response.content[0].text
        self.history.add_turn("assistant", reply)
        return reply


# Usage
async def main():
    client = AsyncAnthropic()
    agent = BudgetedConversationAgent(
        system_prompt="You are a helpful assistant.",
        history_budget=2000,
        max_tokens=256,
    )

    turns = [
        "What is a REST API?",
        "How does authentication work in REST?",
        "What is OAuth 2.0?",
        "Explain JWT tokens.",
        "How do refresh tokens work?",
    ]

    for msg in turns:
        reply = await agent.chat(client, msg)
        print(f"User: {msg}")
        print(f"Agent: {reply[:80]}")
        print(f"History: {agent.history.stats()}\n")

asyncio.run(main())
```

## Solution 3: Per-Section Token Allocation with Priority Cascade

Allocate the total token budget across named sections with defined maximums — sections that exceed their allocation are truncated, and unused budget cascades to lower-priority sections.

```python
import asyncio
from anthropic import AsyncAnthropic, Anthropic
from dataclasses import dataclass


@dataclass
class BudgetAllocation:
    section: str
    max_tokens: int
    actual_tokens: int
    included: bool
    overflow: int = 0  # tokens that couldn't fit


class CascadingBudgetAllocator:
    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.model = model
        self._sync_client = Anthropic()

    def _count(self, text: str) -> int:
        r = self._sync_client.messages.count_tokens(
            model=self.model,
            messages=[{"role": "user", "content": text}],
        )
        return r.input_tokens

    def _truncate_to_budget(self, text: str, budget: int) -> tuple[str, int]:
        """Truncate text to approximately fit within token budget."""
        actual = self._count(text)
        if actual <= budget:
            return text, actual

        # Binary search for right truncation point
        lo, hi = 0, len(text)
        while hi - lo > 20:
            mid = (lo + hi) // 2
            truncated = text[:mid] + "...[truncated]"
            count = self._count(truncated)
            if count <= budget:
                lo = mid
            else:
                hi = mid

        result = text[:lo] + "...[truncated]"
        return result, self._count(result)

    def allocate(
        self,
        sections: dict[str, tuple[str, int]],  # name → (content, max_tokens)
        total_budget: int,
    ) -> tuple[dict[str, str], list[BudgetAllocation], int]:
        """
        Returns (final_sections, allocations, tokens_used).
        Unused budget from earlier sections cascades to later ones.
        """
        remaining = total_budget
        final_sections: dict[str, str] = {}
        allocations: list[BudgetAllocation] = []

        for section_name, (content, max_tokens) in sections.items():
            if remaining <= 0:
                allocations.append(BudgetAllocation(
                    section=section_name, max_tokens=max_tokens,
                    actual_tokens=0, included=False,
                ))
                continue

            # This section's budget: min of its max and remaining
            section_budget = min(max_tokens, remaining)
            actual = self._count(content)

            if actual <= section_budget:
                final_sections[section_name] = content
                remaining -= actual
                allocations.append(BudgetAllocation(
                    section=section_name, max_tokens=max_tokens,
                    actual_tokens=actual, included=True,
                ))
            else:
                # Truncate to budget
                truncated, truncated_tokens = self._truncate_to_budget(content, section_budget)
                final_sections[section_name] = truncated
                overflow = actual - section_budget
                remaining -= truncated_tokens
                allocations.append(BudgetAllocation(
                    section=section_name, max_tokens=max_tokens,
                    actual_tokens=truncated_tokens, included=True, overflow=overflow,
                ))

        tokens_used = total_budget - remaining
        return final_sections, allocations, tokens_used

    def build_prompt(self, sections: dict[str, str], user_message: str) -> str:
        parts = [
            f"<{name}>\n{content}\n</{name}>"
            for name, content in sections.items()
        ]
        if parts:
            return "\n\n".join(parts) + "\n\n" + user_message
        return user_message


# Usage
async def main():
    client = AsyncAnthropic()
    allocator = CascadingBudgetAllocator()

    sections = {
        "system_context": ("Always be helpful and concise.", 200),
        "tool_results": ('{"query": "user data", "results": [{"id": 1, "name": "Alice"}]}', 500),
        "conversation_history": ("Previous turns...\n" * 30, 1000),
        "retrieved_docs": ("Long document content...\n" * 100, 800),
    }

    final_sections, allocations, tokens_used = allocator.allocate(
        sections, total_budget=2000
    )

    for alloc in allocations:
        status = "OK" if alloc.included else "DROPPED"
        overflow = f" overflow={alloc.overflow}" if alloc.overflow else ""
        print(f"  [{status}] {alloc.section}: {alloc.actual_tokens}/{alloc.max_tokens} tokens{overflow}")

    print(f"Total tokens used: {tokens_used}/2000")

    user_message = "Summarize the retrieved documents."
    prompt = allocator.build_prompt(final_sections, user_message)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    print(f"\nResponse: {response.content[0].text[:100]}")

asyncio.run(main())
```

## Solution 4: Extended Thinking Token Budget Control

Use the `budget_tokens` parameter in extended thinking to cap reasoning token consumption, preventing runaway thinking loops from consuming context budget.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class ThinkingBudgetConfig:
    min_budget: int = 1024
    max_budget: int = 10000
    default_budget: int = 3000


def estimate_thinking_budget(
    question_complexity: str,
    config: ThinkingBudgetConfig,
) -> int:
    """
    Route to appropriate thinking budget based on estimated complexity.
    In production: use a lightweight classifier or keyword heuristics.
    """
    complexity_budgets = {
        "simple": config.min_budget,
        "medium": config.default_budget,
        "complex": config.max_budget,
    }
    return complexity_budgets.get(question_complexity, config.default_budget)


async def think_with_budget(
    client: AsyncAnthropic,
    user_message: str,
    complexity: str = "medium",
    config: ThinkingBudgetConfig | None = None,
) -> dict:
    if config is None:
        config = ThinkingBudgetConfig()

    budget = estimate_thinking_budget(complexity, config)

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=budget + 2048,  # thinking + response budget
        thinking={
            "type": "enabled",
            "budget_tokens": budget,
        },
        messages=[{"role": "user", "content": user_message}],
    )

    thinking_tokens = sum(
        len(block.thinking) // 4  # rough estimate
        for block in response.content
        if block.type == "thinking"
    )
    text_blocks = [b.text for b in response.content if b.type == "text"]

    return {
        "complexity": complexity,
        "allocated_thinking_budget": budget,
        "estimated_thinking_tokens": thinking_tokens,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "response": text_blocks[0] if text_blocks else "",
    }


async def adaptive_thinking_agent(
    client: AsyncAnthropic,
    questions: list[tuple[str, str]],  # (question, complexity)
) -> list[dict]:
    results = []
    for question, complexity in questions:
        result = await think_with_budget(client, question, complexity)
        print(f"[{complexity}] budget={result['allocated_thinking_budget']} "
              f"in={result['input_tokens']} out={result['output_tokens']}")
        results.append(result)
    return results


# Usage
async def main():
    client = AsyncAnthropic()
    questions = [
        ("What is 2 + 2?", "simple"),
        ("Explain the tradeoffs between SQL and NoSQL databases.", "medium"),
        ("Design a distributed rate limiting system for 1M requests/second.", "complex"),
    ]

    results = await adaptive_thinking_agent(client, questions)
    for r in results:
        print(f"\n[{r['complexity']}] {r['response'][:120]}")

asyncio.run(main())
```

## Solution 5: Token Budget Middleware with Pre-call Validation

Intercept every API call through a middleware layer that validates token budgets before dispatch — preventing over-budget calls from ever reaching the API.

```python
import asyncio
import functools
from anthropic import AsyncAnthropic, Anthropic
from dataclasses import dataclass, field
from typing import Callable, Any


@dataclass
class BudgetViolation:
    request_tokens: int
    budget: int
    overflow: int
    section_breakdown: dict[str, int]


class TokenBudgetMiddleware:
    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        prompt_token_budget: int = 4096,
        response_token_budget: int = 1024,
        on_violation: str = "truncate",  # "raise" | "truncate" | "warn"
    ):
        self.model = model
        self.prompt_budget = prompt_token_budget
        self.response_budget = response_token_budget
        self.on_violation = on_violation
        self._sync_client = Anthropic()
        self._stats: dict[str, int] = {
            "calls_validated": 0,
            "violations": 0,
            "truncations": 0,
        }

    def _count(self, messages: list[dict], system: str = "") -> int:
        kwargs = {"model": self.model, "messages": messages}
        if system:
            kwargs["system"] = system
        return self._sync_client.messages.count_tokens(**kwargs).input_tokens

    def _truncate_messages(
        self,
        messages: list[dict],
        system: str,
        budget: int,
    ) -> list[dict]:
        """Remove oldest non-system messages until within budget."""
        current = self._count(messages, system)
        mutable = list(messages)

        while current > budget and len(mutable) > 1:
            # Remove oldest user/assistant turn (keep last user message)
            removed = mutable.pop(0)
            print(f"[budget_middleware] Removed oldest message ({removed['role']}) to fit budget")
            current = self._count(mutable, system)

        return mutable

    def validate_and_fix(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int | None = None,
    ) -> tuple[list[dict], str, dict]:
        """Returns (messages, system, report)."""
        self._stats["calls_validated"] += 1

        token_count = self._count(messages, system)
        report = {
            "original_tokens": token_count,
            "budget": self.prompt_budget,
            "within_budget": token_count <= self.prompt_budget,
        }

        if token_count <= self.prompt_budget:
            return messages, system, report

        self._stats["violations"] += 1
        overflow = token_count - self.prompt_budget
        report["overflow"] = overflow

        if self.on_violation == "raise":
            raise BudgetViolation(
                request_tokens=token_count,
                budget=self.prompt_budget,
                overflow=overflow,
                section_breakdown={},
            )
        elif self.on_violation == "truncate":
            messages = self._truncate_messages(messages, system, self.prompt_budget)
            self._stats["truncations"] += 1
            report["truncated"] = True
            report["final_tokens"] = self._count(messages, system)

        return messages, system, report

    async def call(
        self,
        client: AsyncAnthropic,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 512,
        model: str | None = None,
        **kwargs,
    ) -> dict:
        messages, system, report = self.validate_and_fix(messages, system, max_tokens)

        response = await client.messages.create(
            model=model or self.model,
            max_tokens=min(max_tokens, self.response_budget),
            system=system,
            messages=messages,
            **kwargs,
        )

        return {
            "text": response.content[0].text,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "budget_report": report,
        }

    def stats(self) -> dict:
        return self._stats


# Usage
async def main():
    client = AsyncAnthropic()
    middleware = TokenBudgetMiddleware(
        prompt_token_budget=1500,
        response_token_budget=256,
        on_violation="truncate",
    )

    # Build an over-budget conversation
    messages = [
        {"role": "user", "content": f"Question {i}: explain topic {i} in detail." * 5}
        for i in range(10)
    ]
    messages.append({"role": "user", "content": "Summarize everything."})

    result = await middleware.call(
        client,
        messages=messages,
        system="You are a helpful assistant.",
        max_tokens=256,
    )

    print(f"Response: {result['text'][:100]}")
    print(f"Budget report: {result['budget_report']}")
    print(f"Middleware stats: {middleware.stats()}")

asyncio.run(main())
```

## Solution 6: Token-Aware RAG Context Packer

Pack retrieved document chunks into the available context budget in order of relevance score — maximizing information density within the token limit for RAG applications.

```python
import asyncio
from anthropic import AsyncAnthropic, Anthropic
from dataclasses import dataclass, field


@dataclass
class DocumentChunk:
    doc_id: str
    content: str
    relevance_score: float  # 0.0–1.0
    token_count: int = 0


class TokenAwareRAGPacker:
    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        context_budget: int = 3000,
        min_relevance: float = 0.3,
    ):
        self.model = model
        self.context_budget = context_budget
        self.min_relevance = min_relevance
        self._sync_client = Anthropic()

    def _count(self, text: str) -> int:
        return self._sync_client.messages.count_tokens(
            model=self.model,
            messages=[{"role": "user", "content": text}],
        ).input_tokens

    def pack(
        self,
        chunks: list[DocumentChunk],
        query: str,
        reserved_for_query: int = 100,
    ) -> tuple[list[DocumentChunk], dict]:
        """
        Select highest-relevance chunks that fit within context_budget.
        Returns (packed_chunks, packing_report).
        """
        available = self.context_budget - reserved_for_query

        # Filter by minimum relevance, sort by relevance descending
        candidates = sorted(
            [c for c in chunks if c.relevance_score >= self.min_relevance],
            key=lambda c: c.relevance_score,
            reverse=True,
        )

        # Count tokens for each chunk
        for chunk in candidates:
            chunk.token_count = self._count(chunk.content)

        # Greedy pack by relevance
        packed: list[DocumentChunk] = []
        remaining = available
        dropped: list[str] = []

        for chunk in candidates:
            if chunk.token_count <= remaining:
                packed.append(chunk)
                remaining -= chunk.token_count
            else:
                dropped.append(f"{chunk.doc_id}(score={chunk.relevance_score:.2f})")

        report = {
            "context_budget": self.context_budget,
            "chunks_available": len(candidates),
            "chunks_packed": len(packed),
            "chunks_dropped": len(dropped),
            "tokens_packed": available - remaining,
            "tokens_remaining": remaining,
            "utilization": round((available - remaining) / available, 3),
            "dropped": dropped,
        }

        return packed, report

    def build_rag_prompt(
        self,
        packed_chunks: list[DocumentChunk],
        query: str,
    ) -> str:
        if not packed_chunks:
            return query

        context = "\n\n".join([
            f"[Doc {c.doc_id} | relevance={c.relevance_score:.2f}]\n{c.content}"
            for c in packed_chunks
        ])
        return f"<context>\n{context}\n</context>\n\nQuestion: {query}"


# Usage
async def main():
    client = AsyncAnthropic()
    packer = TokenAwareRAGPacker(
        model="claude-haiku-4-5-20251001",
        context_budget=2000,
        min_relevance=0.4,
    )

    # Simulated retrieved chunks with relevance scores
    chunks = [
        DocumentChunk("doc_01", "REST APIs use HTTP methods like GET and POST." * 5, relevance_score=0.95),
        DocumentChunk("doc_02", "GraphQL allows clients to request specific fields." * 8, relevance_score=0.82),
        DocumentChunk("doc_03", "gRPC uses Protocol Buffers for efficient serialization." * 6, relevance_score=0.71),
        DocumentChunk("doc_04", "WebSockets enable real-time bidirectional communication." * 4, relevance_score=0.65),
        DocumentChunk("doc_05", "SOAP is an older XML-based protocol for web services." * 10, relevance_score=0.35),
        DocumentChunk("doc_06", "Unrelated content about database normalization." * 15, relevance_score=0.12),
    ]

    query = "Compare REST and GraphQL for building APIs."
    packed, report = packer.pack(chunks, query)

    print(f"Packing report: {report}")
    prompt = packer.build_rag_prompt(packed, query)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    print(f"\nRAG Response: {response.content[0].text[:200]}")

asyncio.run(main())
```

## Comparison

| Approach | Token Visibility | Enforcement | Dynamic | Overhead | Best For |
|---|---|---|---|---|---|
| Token-Counting Builder | Per-section | Hard budget | No | Low | Multi-section prompt construction |
| Rolling History Window | Per-turn | Eviction | Yes | Low | Long conversations |
| Priority Cascade | Per-section | Truncation | No | Medium | Structured prompt templates |
| Extended Thinking Budget | Reasoning only | API param | Yes | None | Claude extended thinking calls |
| Budget Middleware | Whole prompt | Pre-call | Yes | Low | Drop-in protection for any call |
| Token-Aware RAG Packer | Per-chunk | Greedy pack | Yes | Medium | Retrieval-augmented generation |
