---
layout: solution
title: "Agent Doesn't Implement Prompt Chaining with State Handoff"
category: prompt-engineering
description: "Break complex tasks into sequential prompt stages where each stage's output is structured for clean handoff to the next—enabling specialization, error isolation, and testable pipeline stages."
tags: [prompt-chaining, pipeline, state-handoff, multi-stage, structured-output]
---

# Agent Doesn't Implement Prompt Chaining with State Handoff

## Problem

Trying to accomplish complex multi-step tasks in a single prompt leads to unreliable outputs, poor error isolation, and inability to test or debug individual stages. A single monolithic prompt for "research → analyze → write → format" fails unpredictably when any sub-task goes wrong.

## Solution Options

### Option 1: Linear Chain with Typed State Objects

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ResearchState:
    topic: str
    key_facts: list[str]
    open_questions: list[str]

@dataclass
class AnalysisState:
    topic: str
    key_facts: list[str]
    main_insight: str
    supporting_points: list[str]
    confidence: str  # high / medium / low

@dataclass
class WritingState:
    topic: str
    title: str
    introduction: str
    body_paragraphs: list[str]
    conclusion: str

def parse_json_block(text: str) -> dict:
    match = re.search(r'```json\s*(.*?)```', text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No JSON found in: {text[:200]}")

def stage_research(topic: str) -> ResearchState:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Research the topic: "{topic}"

Output JSON:
```json
{{"key_facts": ["fact1", "fact2", "fact3"], "open_questions": ["question1", "question2"]}}
```"""
        }]
    )
    data = parse_json_block(resp.content[0].text)
    print(f"[Stage 1: Research] {len(data['key_facts'])} facts, {len(data['open_questions'])} open questions")
    return ResearchState(topic=topic, key_facts=data["key_facts"], open_questions=data["open_questions"])

def stage_analyze(research: ResearchState) -> AnalysisState:
    facts_text = "\n".join(f"- {f}" for f in research.key_facts)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Analyze these facts about "{research.topic}":
{facts_text}

Output JSON:
```json
{{"main_insight": "...", "supporting_points": ["point1", "point2"], "confidence": "high|medium|low"}}
```"""
        }]
    )
    data = parse_json_block(resp.content[0].text)
    print(f"[Stage 2: Analysis] confidence={data['confidence']}, {len(data['supporting_points'])} points")
    return AnalysisState(
        topic=research.topic,
        key_facts=research.key_facts,
        main_insight=data["main_insight"],
        supporting_points=data["supporting_points"],
        confidence=data["confidence"]
    )

def stage_write(analysis: AnalysisState) -> WritingState:
    points_text = "\n".join(f"- {p}" for p in analysis.supporting_points)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Write a short article about "{analysis.topic}".
Main insight: {analysis.main_insight}
Supporting points:
{points_text}

Output JSON:
```json
{{"title": "...", "introduction": "...", "body_paragraphs": ["para1", "para2"], "conclusion": "..."}}
```"""
        }]
    )
    data = parse_json_block(resp.content[0].text)
    print(f"[Stage 3: Writing] title='{data['title']}', {len(data['body_paragraphs'])} paragraphs")
    return WritingState(
        topic=analysis.topic,
        title=data["title"],
        introduction=data["introduction"],
        body_paragraphs=data["body_paragraphs"],
        conclusion=data["conclusion"]
    )

# Run the chain
research = stage_research("the CAP theorem in distributed systems")
analysis = stage_analyze(research)
article = stage_write(analysis)
print(f"\n=== {article.title} ===\n{article.introduction[:200]}...")

# Expected Token Savings: specialization allows haiku for research/analysis, sonnet only for writing
# Environment: content pipelines, report generation, structured research agents
```

### Option 2: Conditional Branching Chain

```python
import anthropic
import json
import re
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic()

class QueryType(Enum):
    FACTUAL = "factual"
    ANALYTICAL = "analytical"
    CREATIVE = "creative"
    UNKNOWN = "unknown"

@dataclass
class ClassifiedQuery:
    original: str
    query_type: QueryType
    complexity: str  # simple / moderate / complex
    requires_examples: bool

def classify_query(user_input: str) -> ClassifiedQuery:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"""Classify this query: "{user_input}"

JSON: {{"query_type": "factual|analytical|creative", "complexity": "simple|moderate|complex", "requires_examples": true|false}}"""
        }]
    )
    text = resp.content[0].text
    match = re.search(r'\{.*\}', text, re.DOTALL)
    data = json.loads(match.group()) if match else {}
    qt = QueryType(data.get("query_type", "unknown")) if data.get("query_type") in [e.value for e in QueryType] else QueryType.UNKNOWN
    return ClassifiedQuery(
        original=user_input,
        query_type=qt,
        complexity=data.get("complexity", "simple"),
        requires_examples=data.get("requires_examples", False)
    )

def handle_factual(query: ClassifiedQuery) -> str:
    model = "claude-haiku-4-5-20251001" if query.complexity == "simple" else "claude-sonnet-4-6"
    resp = client.messages.create(
        model=model,
        max_tokens=256,
        system="Answer factual questions precisely and concisely.",
        messages=[{"role": "user", "content": query.original}]
    )
    return resp.content[0].text

def handle_analytical(query: ClassifiedQuery) -> str:
    # Step 1: break down the question
    decomp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Break '{query.original}' into 3 sub-questions. One per line."
        }]
    )
    sub_questions = decomp.content[0].text

    # Step 2: synthesize answers
    synthesis = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Answer this analytically by addressing:\n{sub_questions}\n\nOriginal: {query.original}"
        }]
    )
    return synthesis.content[0].text

def handle_creative(query: ClassifiedQuery) -> str:
    # Brainstorm then refine
    ideas = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Brainstorm 5 creative angles for: {query.original}"}]
    )
    refined = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Pick the best angle from these ideas and execute it fully:\n{ideas.content[0].text}"
        }]
    )
    return refined.content[0].text

def route_and_execute(user_input: str) -> str:
    query = classify_query(user_input)
    print(f"[Classify] type={query.query_type.value} complexity={query.complexity}")

    if query.query_type == QueryType.FACTUAL:
        return handle_factual(query)
    elif query.query_type == QueryType.ANALYTICAL:
        return handle_analytical(query)
    elif query.query_type == QueryType.CREATIVE:
        return handle_creative(query)
    else:
        return handle_factual(query)  # fallback

for q in [
    "What year was the internet invented?",
    "What are the tradeoffs between microservices and monoliths?",
    "Write a metaphor explaining distributed consensus."
]:
    result = route_and_execute(q)
    print(f"Q: {q}\nA: {result[:120]}...\n")

# Expected Token Savings: factual simple queries use haiku only; analytical uses haiku+sonnet
# Environment: general-purpose assistants, multi-domain routers, cost-optimized pipelines
```

### Option 3: Map-Reduce Chain for Document Processing

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ChunkSummary:
    chunk_index: int
    key_points: list[str]
    entities: list[str]
    sentiment: str

@dataclass
class FinalReport:
    executive_summary: str
    key_themes: list[str]
    all_entities: list[str]
    overall_sentiment: str
    chunk_count: int

def chunk_document(text: str, chunk_size: int = 500) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunks.append(" ".join(words[i:i + chunk_size]))
    return chunks

def map_chunk(chunk: str, index: int) -> ChunkSummary:
    """Map stage: process each chunk independently."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Analyze this text chunk:
{chunk[:600]}

List: 3 key points, any named entities, overall sentiment (positive/neutral/negative).
Format: KEY_POINTS: p1|p2|p3\nENTITIES: e1|e2\nSENTIMENT: neutral"""
        }]
    )
    text = resp.content[0].text
    points = []
    entities = []
    sentiment = "neutral"
    for line in text.split("\n"):
        if line.startswith("KEY_POINTS:"):
            points = [p.strip() for p in line.replace("KEY_POINTS:", "").split("|") if p.strip()]
        elif line.startswith("ENTITIES:"):
            entities = [e.strip() for e in line.replace("ENTITIES:", "").split("|") if e.strip()]
        elif line.startswith("SENTIMENT:"):
            sentiment = line.replace("SENTIMENT:", "").strip().lower()
    return ChunkSummary(chunk_index=index, key_points=points, entities=entities, sentiment=sentiment)

def reduce_summaries(summaries: list[ChunkSummary], topic: str) -> FinalReport:
    """Reduce stage: synthesize all chunk summaries into final report."""
    all_points = []
    all_entities: set[str] = set()
    sentiments = []
    for s in summaries:
        all_points.extend(s.key_points)
        all_entities.update(s.entities)
        sentiments.append(s.sentiment)

    majority_sentiment = max(set(sentiments), key=sentiments.count) if sentiments else "neutral"
    points_text = "\n".join(f"- {p}" for p in all_points[:15])

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Synthesize these key points from a document about "{topic}":
{points_text}

Output:
SUMMARY: <2-3 sentence executive summary>
THEMES: theme1|theme2|theme3"""
        }]
    )
    text = resp.content[0].text
    summary = ""
    themes = []
    for line in text.split("\n"):
        if line.startswith("SUMMARY:"):
            summary = line.replace("SUMMARY:", "").strip()
        elif line.startswith("THEMES:"):
            themes = [t.strip() for t in line.replace("THEMES:", "").split("|") if t.strip()]

    return FinalReport(
        executive_summary=summary,
        key_themes=themes,
        all_entities=sorted(all_entities)[:10],
        overall_sentiment=majority_sentiment,
        chunk_count=len(summaries)
    )

# Demo
document = """
Distributed systems present unique challenges for engineering teams. Consistency and availability
are often in tension, as described by the CAP theorem. Apache Kafka has emerged as a leading
solution for event streaming, while Redis provides fast in-memory caching. Netflix and Google
have pioneered many patterns for distributed architectures. Microservices enable independent
scaling but introduce operational complexity. Service meshes like Istio handle cross-cutting
concerns such as observability, security, and traffic management. Kubernetes has become the
de facto standard for container orchestration. The CNCF ecosystem continues to grow with
projects like Prometheus, Grafana, and Jaeger enabling observability. Engineers must balance
complexity against operational burden when designing distributed systems.
""" * 3  # simulate longer document

chunks = chunk_document(document, chunk_size=80)
print(f"Processing {len(chunks)} chunks...")
summaries = [map_chunk(chunk, i) for i, chunk in enumerate(chunks)]
report = reduce_summaries(summaries, "distributed systems")

print(f"\n=== Report ({report.chunk_count} chunks) ===")
print(f"Summary: {report.executive_summary[:200]}")
print(f"Themes: {report.key_themes}")
print(f"Entities: {report.all_entities}")

# Expected Token Savings: map uses haiku (cheap), reduce uses sonnet only once; ~50% vs all-sonnet
# Environment: document analysis pipelines, RAG pre-processing, report generation
```

### Option 4: Retry-Enabled Chain with Stage Validation

```python
import anthropic
import json
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str]

def validate_stage_output(stage_name: str, output: dict) -> ValidationResult:
    required_fields = {
        "outline": ["title", "sections"],
        "draft": ["content", "word_count"],
        "review": ["approved", "issues"],
    }
    errors = []
    for field in required_fields.get(stage_name, []):
        if field not in output:
            errors.append(f"Missing required field: {field}")
    if stage_name == "outline" and "sections" in output:
        if not isinstance(output["sections"], list) or len(output["sections"]) < 2:
            errors.append("sections must be a list with at least 2 items")
    return ValidationResult(is_valid=len(errors) == 0, errors=errors)

def call_stage(stage_name: str, prompt: str, model: str = "claude-haiku-4-5-20251001",
               max_retries: int = 2) -> dict:
    for attempt in range(max_retries + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match:
            if attempt < max_retries:
                print(f"  [Stage:{stage_name}] No JSON found, retry {attempt+1}")
                continue
            return {}
        try:
            data = json.loads(match.group())
            validation = validate_stage_output(stage_name, data)
            if validation.is_valid:
                print(f"  [Stage:{stage_name}] OK on attempt {attempt+1}")
                return data
            else:
                print(f"  [Stage:{stage_name}] Validation failed: {validation.errors}")
                if attempt < max_retries:
                    prompt += f"\n\nPrevious output was invalid: {validation.errors}. Fix and retry."
        except json.JSONDecodeError:
            if attempt < max_retries:
                print(f"  [Stage:{stage_name}] JSON parse error, retry {attempt+1}")
    return {}

def writing_pipeline(topic: str) -> str:
    # Stage 1: Outline
    outline_data = call_stage("outline", f"""Create an outline for an article about "{topic}".
JSON: {{"title": "...", "sections": ["section1", "section2", "section3"]}}""")

    if not outline_data:
        return "Pipeline failed at outline stage."

    # Stage 2: Draft
    sections_text = ", ".join(outline_data.get("sections", []))
    draft_data = call_stage("draft", f"""Write a short article with title "{outline_data['title']}"
covering sections: {sections_text}
JSON: {{"content": "full article text here...", "word_count": 200}}""",
        model="claude-sonnet-4-6")

    if not draft_data:
        return "Pipeline failed at draft stage."

    # Stage 3: Review
    review_data = call_stage("review", f"""Review this article for quality:
{draft_data.get('content', '')[:500]}
JSON: {{"approved": true, "issues": []}}""")

    if review_data.get("approved"):
        return draft_data.get("content", "No content generated.")
    else:
        issues = review_data.get("issues", [])
        return f"Article needs revision: {issues}\n\n{draft_data.get('content', '')}"

result = writing_pipeline("event-driven architecture in microservices")
print(f"\nFinal article:\n{result[:300]}...")

# Expected Token Savings: validation catches bad outputs before they propagate; reduces cascade failures
# Environment: content automation, document generation, any multi-stage structured pipeline
```

### Option 5: Async Parallel Chain with Join Stage

```python
import anthropic
import asyncio
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

@dataclass
class PerspectiveAnalysis:
    perspective: str
    arguments: list[str]
    strength: str  # weak / moderate / strong

@dataclass
class BalancedAnalysis:
    topic: str
    perspectives: list[PerspectiveAnalysis]
    synthesis: str
    recommendation: str

async def analyze_perspective(topic: str, perspective: str) -> PerspectiveAnalysis:
    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"""Analyze "{topic}" from the {perspective} perspective.
List 3 arguments and rate overall strength as weak/moderate/strong.
Format: ARG1: ...\nARG2: ...\nARG3: ...\nSTRENGTH: moderate"""
        }]
    )
    text = resp.content[0].text
    args = []
    strength = "moderate"
    for line in text.split("\n"):
        if line.startswith(("ARG1:", "ARG2:", "ARG3:")):
            args.append(line.split(":", 1)[1].strip())
        elif line.startswith("STRENGTH:"):
            strength = line.replace("STRENGTH:", "").strip().lower()
    return PerspectiveAnalysis(perspective=perspective, arguments=args, strength=strength)

async def synthesize_perspectives(topic: str, analyses: list[PerspectiveAnalysis]) -> tuple[str, str]:
    persp_text = "\n\n".join(
        f"{a.perspective} ({a.strength}):\n" + "\n".join(f"- {arg}" for arg in a.arguments)
        for a in analyses
    )
    resp = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"""Synthesize these perspectives on "{topic}":
{persp_text}

Provide a balanced synthesis and concrete recommendation.
SYNTHESIS: ...
RECOMMENDATION: ..."""
        }]
    )
    text = resp.content[0].text
    synthesis = recommendation = ""
    for line in text.split("\n"):
        if line.startswith("SYNTHESIS:"):
            synthesis = line.replace("SYNTHESIS:", "").strip()
        elif line.startswith("RECOMMENDATION:"):
            recommendation = line.replace("RECOMMENDATION:", "").strip()
    return synthesis, recommendation

async def parallel_perspective_chain(topic: str, perspectives: list[str]) -> BalancedAnalysis:
    print(f"Analyzing {len(perspectives)} perspectives in parallel...")

    # Map: all perspectives in parallel
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(analyze_perspective(topic, p)) for p in perspectives]

    analyses = [t.result() for t in tasks]
    for a in analyses:
        print(f"  [{a.perspective}] strength={a.strength} args={len(a.arguments)}")

    # Reduce: synthesize
    synthesis, recommendation = await synthesize_perspectives(topic, analyses)

    return BalancedAnalysis(
        topic=topic,
        perspectives=analyses,
        synthesis=synthesis,
        recommendation=recommendation
    )

result = asyncio.run(parallel_perspective_chain(
    "adopting GraphQL over REST APIs",
    ["developer experience", "performance", "security", "operational complexity"]
))
print(f"\nSynthesis: {result.synthesis[:200]}")
print(f"Recommendation: {result.recommendation[:150]}")

# Expected Token Savings: parallel map cuts wall time by ~75%; haiku for map, sonnet once for reduce
# Environment: multi-perspective analysis, debate frameworks, decision support systems
```

### Option 6: Event-Driven Chain with State Machine

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

client = anthropic.Anthropic()

class PipelineState(Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    COMPLETE = "complete"
    FAILED = "failed"

@dataclass
class PipelineContext:
    goal: str
    state: PipelineState = PipelineState.IDLE
    plan: list[str] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    final_output: str = ""
    error: str = ""

    def transition(self, new_state: PipelineState) -> None:
        print(f"  [SM] {self.state.value} -> {new_state.value}")
        self.state = new_state

def stage_plan(ctx: PipelineContext) -> PipelineContext:
    ctx.transition(PipelineState.PLANNING)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Create exactly 3 concrete steps to accomplish: {ctx.goal}\nOne step per line, no numbering."
        }]
    )
    ctx.plan = [line.strip() for line in resp.content[0].text.strip().split("\n") if line.strip()][:3]
    print(f"  Plan: {ctx.plan}")
    return ctx

def stage_execute(ctx: PipelineContext) -> PipelineContext:
    ctx.transition(PipelineState.EXECUTING)
    for step in ctx.plan:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"Execute this step for goal '{ctx.goal[:50]}':\n{step}\n\nProvide a concrete output."
            }]
        )
        result = resp.content[0].text
        ctx.results.append(result)
        print(f"  Step '{step[:40]}': {result[:50]}...")
    return ctx

def stage_review(ctx: PipelineContext) -> PipelineContext:
    ctx.transition(PipelineState.REVIEWING)
    results_text = "\n\n".join(f"Step {i+1}: {r}" for i, r in enumerate(ctx.results))
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Goal: {ctx.goal}\n\nStep results:\n{results_text}\n\nSynthesize into final coherent output."
        }]
    )
    ctx.final_output = resp.content[0].text
    ctx.transition(PipelineState.COMPLETE)
    return ctx

TRANSITIONS: dict[PipelineState, Callable] = {
    PipelineState.IDLE: stage_plan,
    PipelineState.PLANNING: stage_execute,
    PipelineState.EXECUTING: stage_review,
}

def run_pipeline(goal: str) -> str:
    ctx = PipelineContext(goal=goal)
    print(f"\nGoal: {goal}")

    while ctx.state not in (PipelineState.COMPLETE, PipelineState.FAILED):
        handler = TRANSITIONS.get(ctx.state)
        if not handler:
            break
        try:
            ctx = handler(ctx)
        except Exception as e:
            ctx.error = str(e)
            ctx.transition(PipelineState.FAILED)
            return f"Pipeline failed: {ctx.error}"

    return ctx.final_output

output = run_pipeline("Create a technical overview of Redis as a caching layer")
print(f"\nOutput:\n{output[:300]}")

# Expected Token Savings: state machine prevents duplicate stage execution; haiku for plan/exec, sonnet for review
# Environment: autonomous task agents, workflow automation, multi-step code generation
```

## Comparison

| Option | Pattern | Parallelism | Error Handling | Best For |
|--------|---------|-------------|----------------|----------|
| 1 | Linear typed stages | None | None | Content pipelines |
| 2 | Conditional routing | None | None | Multi-domain routers |
| 3 | Map-reduce | Map parallel | None | Document processing |
| 4 | Retry with validation | None | Per-stage retry | Production reliability |
| 5 | Async parallel + join | Full parallel | TaskGroup | Multi-perspective analysis |
| 6 | State machine | None | State transition | Autonomous agents |
