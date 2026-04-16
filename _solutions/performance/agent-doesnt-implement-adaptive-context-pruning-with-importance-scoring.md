---
title: "Agent Doesn't Implement Adaptive Context Pruning with Importance Scoring"
description: "Agents that append every message and tool result to the context window without pruning hit the token limit and fail, or silently truncate from the start — discarding the system prompt or early instructions. Implement importance-scored context pruning that assigns relevance scores to each context block, retains the highest-scoring blocks within a token budget, and re-computes scores as the conversation progresses."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-adaptive-context-pruning-with-importance-scoring
tags: [context-pruning, token-budget, importance-scoring, context-management, long-context, memory-management]
symptoms:
  - "Agent hits context window limit mid-conversation and fails with a token overflow error"
  - "Truncation from the start removes the system prompt — agent forgets its instructions"
  - "Tool results from 20 turns ago occupy tokens that should hold recent conversation"
  - "No control over which messages are retained when the context fills up"
  - "Agent repeats questions it already asked because relevant answers were pruned"
---

## Why This Happens

Context management is an afterthought in most agent implementations: messages are appended until the window is full, then either the call fails or the oldest messages are silently dropped. Neither outcome is correct. The right approach is proactive pruning: score each context block by recency, role importance, reference frequency, and content type, then keep the highest-scoring blocks that fit within a configurable token budget. Scores are re-computed incrementally as new messages arrive — a tool result that was referenced twice becomes more important than one that was never cited.

## Solution 1: Context Block Model

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BlockRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    TOOL_CALL = "tool_call"
    SUMMARY = "summary"


# Baseline importance by role — system prompt is never pruned
ROLE_BASE_SCORE: Dict[BlockRole, float] = {
    BlockRole.SYSTEM: float("inf"),    # pinned
    BlockRole.USER: 1.0,
    BlockRole.ASSISTANT: 0.8,
    BlockRole.TOOL_CALL: 0.5,
    BlockRole.TOOL_RESULT: 0.5,
    BlockRole.SUMMARY: 1.5,            # summaries carry compressed history
}


@dataclass
class ContextBlock:
    block_id: str
    role: BlockRole
    content: str
    token_count: int
    turn_index: int                   # conversation turn when this was added
    created_at: float = field(default_factory=time.time)
    reference_count: int = 0          # times cited in later messages
    pinned: bool = False              # pinned blocks are never pruned
    importance_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_pinnable(self) -> bool:
        return self.role == BlockRole.SYSTEM or self.pinned
```

## Solution 2: Importance Scorer

```python
import math
import time
from typing import List


class ContextImportanceScorer:
    """
    Computes an importance score for each context block.
    Score components:
      - role_weight: fixed per role (system = inf, summary = 1.5, etc.)
      - recency: exponential decay from most recent turn
      - reference_boost: +0.3 per citation in later messages
      - content_density: log of token count (dense blocks carry more info)
    """

    def __init__(
        self,
        recency_half_life_turns: float = 10.0,
        reference_boost: float = 0.3,
        density_weight: float = 0.1,
    ):
        self._half_life = recency_half_life_turns
        self._ref_boost = reference_boost
        self._density_weight = density_weight

    def score(self, block: ContextBlock, current_turn: int) -> float:
        if block.is_pinnable:
            return float("inf")

        role_weight = ROLE_BASE_SCORE.get(block.role, 0.5)
        turns_ago = max(current_turn - block.turn_index, 0)
        recency = math.exp(-math.log(2) * turns_ago / self._half_life)
        ref_bonus = block.reference_count * self._ref_boost
        density = self._density_weight * math.log1p(block.token_count)

        return role_weight * recency + ref_bonus + density

    def score_all(
        self, blocks: List[ContextBlock], current_turn: int
    ) -> List[ContextBlock]:
        for block in blocks:
            block.importance_score = self.score(block, current_turn)
        return blocks
```

## Solution 3: Token-Budget Pruner

```python
from typing import List, Tuple


class TokenBudgetPruner:
    """
    Selects the subset of context blocks that fits within a token budget
    while maximising total importance score.
    Pinned blocks (system prompt, explicit pins) are always included.
    Non-pinned blocks are sorted by importance and filled greedily.
    """

    def __init__(
        self,
        token_budget: int,
        overhead_tokens: int = 200,   # reserved for generation + formatting
    ):
        self._budget = token_budget - overhead_tokens

    def prune(
        self, blocks: List[ContextBlock]
    ) -> Tuple[List[ContextBlock], List[ContextBlock]]:
        """
        Returns (retained, pruned) — both lists in original turn order.
        """
        pinned = [b for b in blocks if b.is_pinnable]
        unpinned = [b for b in blocks if not b.is_pinnable]

        pinned_tokens = sum(b.token_count for b in pinned)
        remaining_budget = self._budget - pinned_tokens

        if remaining_budget <= 0:
            # Only pinned blocks fit — prune everything else
            pruned = unpinned
            return sorted(pinned, key=lambda b: b.turn_index), pruned

        # Greedy selection by descending importance score
        sorted_unpinned = sorted(
            unpinned, key=lambda b: b.importance_score, reverse=True
        )
        retained_ids = set()
        used = 0
        for block in sorted_unpinned:
            if used + block.token_count <= remaining_budget:
                retained_ids.add(block.block_id)
                used += block.token_count

        retained = [b for b in blocks if b.is_pinnable or b.block_id in retained_ids]
        pruned = [b for b in unpinned if b.block_id not in retained_ids]

        # Preserve chronological order
        retained.sort(key=lambda b: b.turn_index)
        return retained, pruned

    def utilization(self, blocks: List[ContextBlock]) -> float:
        used = sum(b.token_count for b in blocks)
        return round(used / self._budget, 4)
```

## Solution 4: Reference Tracker

```python
import re
from typing import List


class ReferenceTracker:
    """
    Scans new messages for references to content from earlier blocks.
    Increments reference_count on blocks that are cited, boosting their
    importance score and making them more likely to survive pruning.
    """

    def __init__(self, min_phrase_length: int = 4):
        self._min_length = min_phrase_length

    def update_references(
        self,
        new_content: str,
        existing_blocks: List[ContextBlock],
    ) -> int:
        updated = 0
        lower_new = new_content.lower()
        for block in existing_blocks:
            if block.is_pinnable:
                continue
            # Extract key phrases: words ≥ min_length
            phrases = re.findall(r"\b\w{%d,}\b" % self._min_length, block.content.lower())
            unique_phrases = list(set(phrases))[:20]   # cap to avoid O(n²)
            for phrase in unique_phrases:
                if phrase in lower_new:
                    block.reference_count += 1
                    updated += 1
                    break   # one hit per block per message is enough
        return updated
```

## Solution 5: Context Summarizer

```python
from typing import List, Optional


class ContextSummarizer:
    """
    Replaces a group of low-importance blocks with a single summary block.
    The summary preserves key facts in fewer tokens, freeing budget for
    recent, high-importance content.
    In production, call an LLM to generate the summary; here we provide
    the interface with a placeholder that truncates intelligently.
    """

    def __init__(
        self,
        summarize_fn=None,    # async fn(text: str) -> str
        compression_ratio: float = 0.3,
    ):
        self._summarize_fn = summarize_fn
        self._ratio = compression_ratio
        self._summary_counter = 0

    async def summarize_blocks(
        self,
        blocks: List[ContextBlock],
        current_turn: int,
    ) -> Optional[ContextBlock]:
        if not blocks:
            return None

        combined = "\n".join(
            f"[{b.role.value}]: {b.content}" for b in blocks
        )
        if self._summarize_fn:
            summary_text = await self._summarize_fn(combined)
        else:
            # Fallback: keep first sentence of each block
            lines = []
            for b in blocks:
                first = b.content.split(".")[0].strip()
                if first:
                    lines.append(f"{b.role.value}: {first}.")
            summary_text = " ".join(lines)

        target_tokens = max(
            int(sum(b.token_count for b in blocks) * self._ratio), 20
        )
        # Truncate to target (rough: 4 chars ≈ 1 token)
        summary_text = summary_text[: target_tokens * 4]

        self._summary_counter += 1
        return ContextBlock(
            block_id=f"summary-{self._summary_counter}",
            role=BlockRole.SUMMARY,
            content=summary_text,
            token_count=max(len(summary_text) // 4, 1),
            turn_index=min(b.turn_index for b in blocks),
            metadata={"summarizes_block_ids": [b.block_id for b in blocks]},
        )
```

## Solution 6: Adaptive Context Manager

```python
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


class AdaptiveContextManager:
    """
    Orchestrates context pruning across the full conversation.
    On each new message, scores all blocks, prunes to budget,
    and optionally compresses clusters of low-importance blocks.
    """

    def __init__(
        self,
        token_budget: int,
        scorer: ContextImportanceScorer,
        pruner: TokenBudgetPruner,
        ref_tracker: ReferenceTracker,
        summarizer: Optional[ContextSummarizer] = None,
        summarize_threshold: int = 5,   # prune ≥ N blocks → summarize instead
    ):
        self._budget = token_budget
        self._scorer = scorer
        self._pruner = pruner
        self._ref = ref_tracker
        self._summarizer = summarizer
        self._summarize_threshold = summarize_threshold
        self._blocks: List[ContextBlock] = []
        self._current_turn = 0
        self._pruned_count = 0
        self._summarized_count = 0

    def add_block(
        self,
        role: BlockRole,
        content: str,
        token_count: int,
        pinned: bool = False,
        metadata: dict = None,
    ) -> ContextBlock:
        self._current_turn += 1
        block = ContextBlock(
            block_id=str(uuid.uuid4())[:8],
            role=role,
            content=content,
            token_count=token_count,
            turn_index=self._current_turn,
            pinned=pinned,
            metadata=metadata or {},
        )
        # Update references in existing blocks
        self._ref.update_references(content, self._blocks)
        self._blocks.append(block)
        return block

    async def fit_to_budget(self) -> Dict[str, Any]:
        # Score
        self._scorer.score_all(self._blocks, self._current_turn)
        # Prune
        retained, pruned = self._pruner.prune(self._blocks)

        if pruned and self._summarizer and len(pruned) >= self._summarize_threshold:
            summary = await self._summarizer.summarize_blocks(
                pruned, self._current_turn
            )
            if summary:
                retained = [summary] + retained
                retained.sort(key=lambda b: b.turn_index)
                self._summarized_count += len(pruned)
        else:
            self._pruned_count += len(pruned)

        self._blocks = retained
        return {
            "retained": len(retained),
            "pruned": len(pruned),
            "utilization": self._pruner.utilization(retained),
            "total_pruned_ever": self._pruned_count,
            "total_summarized_ever": self._summarized_count,
        }

    def current_blocks(self) -> List[ContextBlock]:
        return list(self._blocks)

    def token_usage(self) -> int:
        return sum(b.token_count for b in self._blocks)
```

## Comparison

| Approach | Importance Scoring | Token Budget | Reference Tracking | Summarization |
|---|---|---|---|---|
| ContextImportanceScorer | Yes (recency + refs + density) | No | No | No |
| TokenBudgetPruner | Via scores | Yes (greedy) | No | No |
| ReferenceTracker | No | No | Yes | No |
| ContextSummarizer | No | No | No | Yes (compress) |
| AdaptiveContextManager | Via scorer | Via pruner | Via tracker | Optional |

**Best for production**: Set token budget to 80% of the model's context window to leave room for generation. Pin the system prompt and any standing instructions with `pinned=True`. Use `ReferenceTracker` so that a tool result cited in a later message is not pruned — this is the most common cause of agents "forgetting" relevant information. Enable `ContextSummarizer` with an LLM call for conversations over 30 turns to compress early history rather than discard it. Call `fit_to_budget()` before every API call, not reactively on overflow.
