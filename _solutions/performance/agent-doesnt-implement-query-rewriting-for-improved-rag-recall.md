---
title: "Agent Doesn't Implement Query Rewriting for Improved RAG Recall"
description: "RAG agents that embed the user's literal question as the retrieval query perform poorly when the question is ambiguous, uses different terminology than the corpus, or is conversational rather than declarative. Query rewriting transforms the user's input into one or more optimized retrieval queries that improve recall by matching the vocabulary and structure of indexed documents. Implement query expansion, reformulation, and hypothetical document embedding to increase relevant chunk retrieval."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-query-rewriting-for-improved-rag-recall
tags: [query-rewriting, rag-recall, query-expansion, hyde, query-reformulation, retrieval-quality]
symptoms:
  - "User asks 'how do I fix a broken pipe?' but the corpus uses 'plumbing repair procedures'"
  - "Conversational queries like 'what about the timeout issue?' retrieve nothing useful"
  - "Single-query retrieval misses relevant documents that use different terminology"
  - "No query augmentation — the embedding of the raw user message is the only retrieval signal"
  - "Recall@10 is 40% when rewriting could bring it to 75%"
---

## Why This Happens

Vector search retrieves documents whose embeddings are similar to the query embedding. When the user's vocabulary diverges from the corpus vocabulary — different terminology, conversational phrasing, missing context from conversation history — the query embedding lands in a different semantic neighborhood from the relevant documents. Query rewriting closes this gap by generating multiple reformulations of the user's intent: an expanded version, a keyword-focused version, and optionally a hypothetical answer (HyDE) whose embedding better matches the embedding space of relevant documents.

## Solution 1: Rewritten Query Set

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class RewriteStrategy(str, Enum):
    ORIGINAL = "original"           # baseline: no rewriting
    EXPANDED = "expanded"           # add synonyms and related terms
    KEYWORDS = "keywords"           # extract key noun phrases
    HYPOTHETICAL_DOC = "hyde"       # generate a hypothetical answer passage
    DECOMPOSED = "decomposed"       # break complex query into sub-questions
    CONVERSATION_AWARE = "conv"     # incorporate conversation context


@dataclass
class RewrittenQuery:
    text: str
    strategy: RewriteStrategy
    weight: float = 1.0             # relative weight for result merging
    metadata: dict = field(default_factory=dict)


@dataclass
class QueryRewriteSet:
    original_query: str
    rewrites: List[RewrittenQuery] = field(default_factory=list)
    session_id: str = ""

    def all_texts(self) -> List[str]:
        return [r.text for r in self.rewrites]

    def weighted_texts(self) -> List[tuple]:
        return [(r.text, r.weight) for r in self.rewrites]
```

## Solution 2: LLM-Based Query Rewriter

```python
import asyncio
from typing import Any, Callable, List, Optional


class LLMQueryRewriter:
    """
    Uses a lightweight LLM call to generate query rewrites.
    Each strategy uses a different prompt template.
    Uses a small/fast model (haiku-class) to minimize rewrite latency.
    """

    EXPAND_PROMPT = (
        "Rewrite the following search query to improve document retrieval. "
        "Add synonyms, related terms, and clarify intent. "
        "Return only the rewritten query, nothing else.\n\nQuery: {query}"
    )

    KEYWORDS_PROMPT = (
        "Extract the key search terms from this query as a concise keyword string "
        "suitable for document retrieval. Return only the keywords.\n\nQuery: {query}"
    )

    HYDE_PROMPT = (
        "Write a short passage (2-3 sentences) that would be a perfect answer to "
        "the following question. This passage will be used to find similar documents.\n\n"
        "Question: {query}"
    )

    DECOMPOSE_PROMPT = (
        "Break this complex question into 2-3 simpler sub-questions that together "
        "cover the original question. Return one sub-question per line.\n\nQuestion: {query}"
    )

    def __init__(
        self,
        llm_fn: Callable[[str], Any],
        strategies: Optional[List[RewriteStrategy]] = None,
        max_concurrent: int = 3,
    ):
        self._llm = llm_fn
        self._strategies = strategies or [
            RewriteStrategy.EXPANDED,
            RewriteStrategy.KEYWORDS,
            RewriteStrategy.HYPOTHETICAL_DOC,
        ]
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def _call(self, prompt: str) -> str:
        async with self._semaphore:
            response = await self._llm(prompt)
            return response.strip() if isinstance(response, str) else str(response).strip()

    async def rewrite(
        self,
        query: str,
        conversation_context: str = "",
    ) -> QueryRewriteSet:
        rewrite_set = QueryRewriteSet(
            original_query=query,
            rewrites=[RewrittenQuery(text=query, strategy=RewriteStrategy.ORIGINAL, weight=1.0)],
        )

        tasks = {}
        for strategy in self._strategies:
            if strategy == RewriteStrategy.EXPANDED:
                tasks[strategy] = asyncio.create_task(
                    self._call(self.EXPAND_PROMPT.format(query=query))
                )
            elif strategy == RewriteStrategy.KEYWORDS:
                tasks[strategy] = asyncio.create_task(
                    self._call(self.KEYWORDS_PROMPT.format(query=query))
                )
            elif strategy == RewriteStrategy.HYPOTHETICAL_DOC:
                tasks[strategy] = asyncio.create_task(
                    self._call(self.HYDE_PROMPT.format(query=query))
                )
            elif strategy == RewriteStrategy.DECOMPOSED:
                tasks[strategy] = asyncio.create_task(
                    self._call(self.DECOMPOSE_PROMPT.format(query=query))
                )

        for strategy, task in tasks.items():
            try:
                result_text = await task
                if strategy == RewriteStrategy.DECOMPOSED:
                    # Each line is a separate sub-query
                    for line in result_text.strip().split("\n"):
                        line = line.strip().lstrip("123.-) ")
                        if line:
                            rewrite_set.rewrites.append(
                                RewrittenQuery(text=line, strategy=strategy, weight=0.7)
                            )
                else:
                    weight = 0.9 if strategy == RewriteStrategy.HYPOTHETICAL_DOC else 0.8
                    rewrite_set.rewrites.append(
                        RewrittenQuery(text=result_text, strategy=strategy, weight=weight)
                    )
            except Exception:
                pass  # strategy failed — continue with others

        return rewrite_set
```

## Solution 3: Multi-Query Retriever

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple


class MultiQueryRetriever:
    """
    Executes retrieval for each rewritten query in parallel,
    then merges and deduplicates results using Reciprocal Rank Fusion.
    """

    def __init__(
        self,
        retrieve_fn: Callable[[str, int], Any],   # (query, top_k) -> List[dict]
        top_k_per_query: int = 10,
        rrf_k: int = 60,
    ):
        self._retrieve = retrieve_fn
        self._top_k = top_k_per_query
        self._rrf_k = rrf_k

    async def retrieve(
        self,
        rewrite_set: QueryRewriteSet,
        final_top_k: int = 10,
    ) -> List[dict]:
        # Fire all queries in parallel
        tasks = [
            asyncio.create_task(
                self._retrieve(rq.text, self._top_k)
            )
            for rq in rewrite_set.rewrites
        ]
        result_lists = await asyncio.gather(*tasks, return_exceptions=True)

        # RRF merging
        scores: Dict[str, float] = {}
        doc_map: Dict[str, dict] = {}

        for i, (rq, results) in enumerate(zip(rewrite_set.rewrites, result_lists)):
            if isinstance(results, Exception):
                continue
            for rank, doc in enumerate(results):
                doc_id = doc.get("id") or doc.get("chunk_id") or str(hash(doc.get("text", "")))
                rrf_score = rq.weight / (self._rrf_k + rank + 1)
                scores[doc_id] = scores.get(doc_id, 0.0) + rrf_score
                doc_map[doc_id] = doc

        # Sort by RRF score and return top_k
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:final_top_k]
        return [
            {**doc_map[doc_id], "rrf_score": round(rrf_score, 6)}
            for doc_id, rrf_score in ranked
        ]
```

## Solution 4: Rewrite Quality Evaluator

```python
import time
from typing import List


class RewriteQualityEvaluator:
    """
    Evaluates whether query rewriting improved retrieval by comparing
    the number of results retrieved before and after rewriting.
    Also tracks which strategies contribute the most unique results.
    """

    def __init__(self):
        self._events: List[dict] = []

    def record(
        self,
        original_result_count: int,
        rewritten_result_count: int,
        strategies_used: List[RewriteStrategy],
        query_length: int,
    ) -> None:
        self._events.append({
            "ts": time.time(),
            "original_count": original_result_count,
            "rewritten_count": rewritten_result_count,
            "improvement": rewritten_result_count - original_result_count,
            "strategies": [s.value for s in strategies_used],
            "query_length": query_length,
        })

    def stats(self) -> dict:
        if not self._events:
            return {"calls": 0}
        avg_improvement = sum(e["improvement"] for e in self._events) / len(self._events)
        improved = sum(1 for e in self._events if e["improvement"] > 0)
        return {
            "calls": len(self._events),
            "avg_result_improvement": round(avg_improvement, 2),
            "pct_improved": round(improved / len(self._events) * 100, 1),
        }
```

## Solution 5: Conversation-Aware Query Enricher

```python
from typing import List, Optional


class ConversationAwareQueryEnricher:
    """
    Enriches a query with relevant context from conversation history
    before rewriting. Resolves pronouns and references ("it", "that approach",
    "the error from before") by prepending recent conversation turns.
    """

    def __init__(self, max_context_turns: int = 3, max_context_chars: int = 500):
        self._max_turns = max_context_turns
        self._max_chars = max_context_chars

    def enrich(
        self,
        query: str,
        conversation_history: Optional[List[dict]] = None,
    ) -> str:
        if not conversation_history:
            return query

        recent = conversation_history[-self._max_turns * 2:]
        context_parts = []
        total_chars = 0
        for turn in reversed(recent):
            content = turn.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict)
                )
            snippet = str(content)[:200]
            if total_chars + len(snippet) > self._max_chars:
                break
            context_parts.insert(0, snippet)
            total_chars += len(snippet)

        if not context_parts:
            return query

        context = " | ".join(context_parts)
        return f"[Context: {context}] {query}"
```

## Solution 6: Query Rewrite Pipeline

```python
import asyncio
import time
from typing import Any, List, Optional


class QueryRewritePipeline:
    """
    Orchestrates enrichment → rewriting → retrieval in a single call.
    Exposes a simple interface: given a query, return ranked chunks.
    """

    def __init__(
        self,
        enricher: ConversationAwareQueryEnricher,
        rewriter: LLMQueryRewriter,
        retriever: MultiQueryRetriever,
        evaluator: RewriteQualityEvaluator,
    ):
        self._enricher = enricher
        self._rewriter = rewriter
        self._retriever = retriever
        self._evaluator = evaluator

    async def retrieve(
        self,
        query: str,
        conversation_history: Optional[List[dict]] = None,
        top_k: int = 10,
    ) -> tuple:  # (chunks, pipeline_report)
        start = time.time()

        enriched = self._enricher.enrich(query, conversation_history)
        rewrite_set = await self._rewriter.rewrite(enriched)
        chunks = await self._retriever.retrieve(rewrite_set, final_top_k=top_k)

        elapsed_ms = round((time.time() - start) * 1000, 1)
        report = {
            "original_query": query,
            "enriched_query": enriched,
            "rewrite_count": len(rewrite_set.rewrites),
            "strategies": [r.strategy for r in rewrite_set.rewrites],
            "chunks_retrieved": len(chunks),
            "pipeline_latency_ms": elapsed_ms,
        }

        self._evaluator.record(
            original_result_count=0,  # not measured here; compare externally
            rewritten_result_count=len(chunks),
            strategies_used=[r.strategy for r in rewrite_set.rewrites],
            query_length=len(query),
        )

        return chunks, report
```

## Comparison

| Approach | Rewrite Strategies | Parallel Retrieval | RRF Merging | Conversation Context | Evaluation |
|---|---|---|---|---|---|
| LLMQueryRewriter | Yes (4 strategies) | Yes (concurrent) | No | No | No |
| MultiQueryRetriever | No | Yes | Yes | No | No |
| ConversationAwareQueryEnricher | No | No | No | Yes | No |
| QueryRewritePipeline | Via rewriter | Via retriever | Via retriever | Via enricher | Via evaluator |
| RewriteQualityEvaluator | No | No | No | No | Yes |

**Best for production**: Enable `EXPANDED` and `KEYWORDS` strategies as the baseline — these have low latency and consistently improve recall for terminology mismatches. Add `HYPOTHETICAL_DOC` (HyDE) for knowledge-base queries where the corpus contains declarative answers; HyDE is particularly effective when users ask questions that are structurally different from how answers are written in the corpus. Set `rrf_k=60` (the standard RRF constant) and `top_k_per_query=10` — with 3 strategies this retrieves up to 30 candidates before RRF merging to your final `top_k`. Monitor `RewriteQualityEvaluator.stats()` for `pct_improved`: below 30% means the rewriting overhead isn't paying off and strategies should be revised.
