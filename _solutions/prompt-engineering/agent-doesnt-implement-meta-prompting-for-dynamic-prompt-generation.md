---
layout: solution
title: "Agent Doesn't Implement Meta-Prompting for Dynamic Prompt Generation"
category: prompt-engineering
description: "Agent uses static hardcoded prompts for all situations — missing the opportunity to let the model generate better task-specific prompts dynamically, improving quality for novel or complex requests."
tags: [prompt-engineering, meta-prompting, dynamic-prompts, self-improvement, prompt-optimization]
---

# Agent Doesn't Implement Meta-Prompting for Dynamic Prompt Generation

## Problem

Static prompts are written once and never adapt. When a user's request falls outside the expected pattern — an unusual domain, a novel task structure, or a request that needs specialized framing — the static prompt is suboptimal.

**Meta-prompting** uses an LLM to generate or refine the prompt for a given task before executing it, producing task-optimized instructions rather than generic ones.

**Root cause:** Prompts are hardcoded strings rather than dynamically composed artifacts.

**Symptoms:**
- Generic-sounding responses for specialized domains
- Inconsistent quality across diverse task types
- Developers spending hours manually iterating prompts for new use cases
- Inability to adapt framing for different user expertise levels

---

## Option 1: Prompt Generator — Ask the Model to Write Its Own System Prompt

Use a meta-model call to generate a task-specific system prompt, then execute the task with it.

```python
import anthropic

client = anthropic.Anthropic()

META_PROMPT_SYSTEM = """You are an expert prompt engineer. Given a task description, write an optimal system prompt for an AI assistant to complete that task.

The system prompt should:
- Define the AI's role and expertise
- Specify the output format
- Include any necessary constraints
- Be concise (< 200 words)

Write ONLY the system prompt text. No commentary, no labels."""

def generate_system_prompt(task_description: str) -> str:
    """Use LLM to generate an optimal system prompt for a given task."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=META_PROMPT_SYSTEM,
        messages=[{"role": "user", "content": f"Task: {task_description}"}]
    )
    return response.content[0].text.strip()

def run_with_meta_prompt(user_query: str) -> dict:
    # Step 1: Classify the task (brief)
    task_type_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{
            "role": "user",
            "content": f"Describe in one sentence what type of task this is: {user_query}"
        }]
    )
    task_description = task_type_response.content[0].text

    # Step 2: Generate optimal system prompt for this task type
    system_prompt = generate_system_prompt(task_description)
    print(f"[meta] Task type: {task_description[:60]}")
    print(f"[meta] Generated system prompt:\n{system_prompt[:150]}...\n")

    # Step 3: Execute with the dynamically generated prompt
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_query}]
    )
    answer = response.content[0].text

    return {
        "task_type": task_description,
        "generated_prompt": system_prompt,
        "answer": answer
    }

test_queries = [
    "Review this Python code for bugs: def fib(n): return fib(n-1) + fib(n-2)",
    "Help me negotiate a salary raise with my manager",
    "Design a database schema for a multi-tenant SaaS application",
]

for q in test_queries:
    print(f"{'='*60}\nQuery: {q[:60]}")
    result = run_with_meta_prompt(q)
    print(f"Answer: {result['answer'][:150]}...\n")

# Expected Token Savings: ~-10% (meta call adds cost; but quality improvement reduces re-prompting)
# Environment: General-purpose agents handling diverse task types across many domains
```

---

## Option 2: Few-Shot Prompt Synthesis — Generate Examples for Novel Tasks

When the agent encounters an unfamiliar task type, generate task-specific few-shot examples dynamically.

```python
import anthropic
import json

client = anthropic.Anthropic()

FEW_SHOT_GENERATOR_SYSTEM = """You are an expert at creating few-shot examples for LLM prompts.

Given a task description, generate 2-3 high-quality input/output examples that demonstrate the ideal response pattern.

Output JSON array:
[{"input": "example user input", "output": "ideal response"}, ...]

Rules:
- Examples should be realistic and diverse
- Outputs should match the format and quality expected
- Keep examples concise but informative"""

def generate_few_shot_examples(task: str, output_format: str = "") -> list[dict]:
    prompt = f"Task: {task}"
    if output_format:
        prompt += f"\nOutput format requirement: {output_format}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=FEW_SHOT_GENERATOR_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return []

def build_few_shot_prompt(task: str, user_query: str, output_format: str = "") -> str:
    examples = generate_few_shot_examples(task, output_format)
    if not examples:
        return user_query

    examples_text = "\n\n".join(
        f"Input: {ex['input']}\nOutput: {ex['output']}"
        for ex in examples
    )
    return f"Examples:\n{examples_text}\n\nNow complete this:\nInput: {user_query}\nOutput:"

def run_with_dynamic_few_shot(task_type: str, user_query: str, output_format: str = "") -> str:
    # Generate few-shot examples dynamically
    enriched_prompt = build_few_shot_prompt(task_type, user_query, output_format)
    print(f"[meta] Enriched prompt ({len(enriched_prompt)} chars)")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": enriched_prompt}]
    )
    return response.content[0].text

# Tasks that benefit from dynamic few-shot
result = run_with_dynamic_few_shot(
    task_type="Convert informal English text to professional business language",
    user_query="the meeting was kinda bad and we didn't really get anywhere tbh",
    output_format="Professional email-appropriate sentence"
)
print(f"Result: {result}")

result2 = run_with_dynamic_few_shot(
    task_type="Extract structured data from unstructured support tickets",
    user_query="User says: my login keeps failing since yesterday morning, tried resetting password twice, still broken",
    output_format='JSON with fields: issue_type, severity, steps_taken, duration'
)
print(f"\nResult 2: {result2}")

# Expected Token Savings: ~-15% (few-shot generation adds 1 call; saves multiple user re-iterations)
# Environment: Agents serving diverse enterprise workflows; data extraction, transformation, classification tasks
```

---

## Option 3: Prompt Optimizer — Iteratively Improve Prompts Against Test Cases

Use the model to evaluate and refine prompts against a set of expected outputs.

```python
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class PromptCandidate:
    prompt: str
    score: float = 0.0
    test_results: list[dict] = field(default_factory=list)

OPTIMIZER_SYSTEM = """You are a prompt optimization expert.

You will receive:
1. A task description
2. The current prompt
3. Test case results (input, expected output, actual output)

Analyze the failures and suggest an improved prompt that would produce better results.

Output JSON:
{"improved_prompt": "<new prompt text>", "changes": "<what you changed and why>"}"""

def score_prompt(prompt: str, test_cases: list[dict]) -> tuple[float, list[dict]]:
    """Evaluate a prompt against test cases. Returns (score, detailed_results)."""
    results = []
    for case in test_cases:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=prompt,
            messages=[{"role": "user", "content": case["input"]}]
        )
        actual = response.content[0].text.strip()

        # Simple scoring: check if expected keywords appear in output
        expected_keywords = case.get("expected_keywords", [])
        hits = sum(1 for kw in expected_keywords if kw.lower() in actual.lower())
        case_score = hits / max(len(expected_keywords), 1)

        results.append({
            "input": case["input"],
            "expected_keywords": expected_keywords,
            "actual": actual[:100],
            "score": case_score,
            "passed": case_score >= 0.7
        })

    avg_score = sum(r["score"] for r in results) / max(len(results), 1)
    return avg_score, results

def optimize_prompt(
    task: str,
    initial_prompt: str,
    test_cases: list[dict],
    max_iterations: int = 3
) -> PromptCandidate:
    current = PromptCandidate(prompt=initial_prompt)
    current.score, current.test_results = score_prompt(current.prompt, test_cases)
    print(f"[optimizer] Initial score: {current.score:.2f}")

    best = current

    for i in range(max_iterations):
        if current.score >= 0.9:
            print(f"[optimizer] Converged at iteration {i}")
            break

        failures = [r for r in current.test_results if not r["passed"]]
        if not failures:
            break

        # Ask LLM to improve the prompt
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=OPTIMIZER_SYSTEM,
            messages=[{
                "role": "user",
                "content": json.dumps({
                    "task": task,
                    "current_prompt": current.prompt,
                    "failures": failures[:3]
                })
            }]
        )

        try:
            suggestion = json.loads(response.content[0].text)
            new_prompt = suggestion.get("improved_prompt", current.prompt)
            changes = suggestion.get("changes", "")
        except json.JSONDecodeError:
            new_prompt = current.prompt
            changes = "Parse error"

        candidate = PromptCandidate(prompt=new_prompt)
        candidate.score, candidate.test_results = score_prompt(candidate.prompt, test_cases)
        print(f"[optimizer] Iteration {i+1}: score={candidate.score:.2f} | {changes[:60]}")

        if candidate.score > best.score:
            best = candidate

        current = candidate

    return best

# Test case: sentiment extraction task
TASK = "Extract sentiment from customer feedback as: POSITIVE, NEGATIVE, or NEUTRAL"
INITIAL_PROMPT = "Analyze the text and tell me the sentiment."
TEST_CASES = [
    {"input": "This product is absolutely amazing!", "expected_keywords": ["POSITIVE"]},
    {"input": "Terrible experience, completely broken.", "expected_keywords": ["NEGATIVE"]},
    {"input": "It arrived on Tuesday.", "expected_keywords": ["NEUTRAL"]},
    {"input": "Works okay but could be better.", "expected_keywords": ["NEUTRAL"]},
]

best_prompt = optimize_prompt(TASK, INITIAL_PROMPT, TEST_CASES, max_iterations=2)
print(f"\n[optimizer] Best prompt (score={best_prompt.score:.2f}):\n{best_prompt.prompt}")

# Expected Token Savings: ~-30% during optimization; ~+20% in production (better prompts = fewer retry turns)
# Environment: Development-time prompt optimization; A/B testing infrastructure; prompt CI/CD pipelines
```

---

## Option 4: Adaptive Persona Injection — Tailor Expertise Level to User

Generate a persona-adapted system prompt based on detected user expertise level.

```python
import anthropic
import json

client = anthropic.Anthropic()

EXPERTISE_DETECTOR = """Analyze the user's message and estimate their expertise level in the relevant domain.

Reply with JSON:
{"level": "beginner|intermediate|expert", "domain": "<domain>", "evidence": "<one sentence explaining your assessment>"}"""

PERSONA_TEMPLATES = {
    "beginner": """You are a patient teacher explaining {domain} to someone new to the subject.
- Use simple language and avoid jargon (define any technical terms you must use)
- Use analogies from everyday life
- Provide step-by-step explanations
- Be encouraging and supportive""",

    "intermediate": """You are a knowledgeable colleague discussing {domain}.
- Use standard technical terminology
- Assume familiarity with basic concepts
- Focus on practical application and best practices
- Skip basic definitions""",

    "expert": """You are a domain expert in {domain} speaking with a peer.
- Use precise technical language and industry terminology
- Discuss nuances, edge cases, and trade-offs
- Reference relevant papers, specs, or standards where appropriate
- Skip all basics — assume deep domain knowledge"""
}

def detect_expertise(query: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=EXPERTISE_DETECTOR,
        messages=[{"role": "user", "content": query}]
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return {"level": "intermediate", "domain": "general", "evidence": "Default"}

def run_adaptive_persona_agent(query: str) -> str:
    # Step 1: Detect expertise
    expertise = detect_expertise(query)
    level = expertise.get("level", "intermediate")
    domain = expertise.get("domain", "the topic")
    print(f"[meta] Expertise: {level} in {domain} — {expertise.get('evidence', '')[:60]}")

    # Step 2: Generate adapted system prompt
    template = PERSONA_TEMPLATES.get(level, PERSONA_TEMPLATES["intermediate"])
    system_prompt = template.format(domain=domain)

    # Step 3: Respond with adapted persona
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=system_prompt,
        messages=[{"role": "user", "content": query}]
    )
    return response.content[0].text

# Queries at different expertise levels
queries = [
    "What is a pointer in C? I just started learning programming.",
    "When should I use smart pointers vs raw pointers in modern C++?",
    "What are the aliasing implications of reinterpret_cast vs C-style casts in UB-safe codebases?",
]

for q in queries:
    print(f"\n{'='*60}\nQuery: {q}")
    answer = run_adaptive_persona_agent(q)
    print(f"Answer: {answer[:200]}...")

# Expected Token Savings: ~-5% (expertise detection is cheap; prevents users from asking follow-up clarification questions)
# Environment: Educational platforms, developer tools, technical documentation assistants
```

---

## Option 5: Task Decomposition Meta-Prompt — Generate a Sub-Task Plan

Use a meta call to decompose a complex task into structured sub-prompts before execution.

```python
import anthropic
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic()

DECOMPOSER_SYSTEM = """You are a task planner for AI agents. Given a complex task, decompose it into ordered sub-tasks.

For each sub-task, specify:
- A specific, actionable instruction
- The expected output format
- Whether it depends on previous sub-tasks

Output JSON:
{"sub_tasks": [{"id": 1, "instruction": "...", "output_format": "...", "depends_on": []}], "synthesis_instruction": "..."}"""

@dataclass
class SubTask:
    id: int
    instruction: str
    output_format: str
    depends_on: list[int]
    result: str = ""

def decompose_task(complex_task: str) -> tuple[list[SubTask], str]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=DECOMPOSER_SYSTEM,
        messages=[{"role": "user", "content": f"Task: {complex_task}"}]
    )
    try:
        data = json.loads(response.content[0].text)
        sub_tasks = [
            SubTask(
                id=st["id"],
                instruction=st["instruction"],
                output_format=st.get("output_format", "text"),
                depends_on=st.get("depends_on", [])
            )
            for st in data.get("sub_tasks", [])
        ]
        synthesis = data.get("synthesis_instruction", "Combine all results into a final answer.")
        return sub_tasks, synthesis
    except (json.JSONDecodeError, KeyError):
        return [], "Summarize the results."

def execute_sub_task(sub_task: SubTask, context: dict[int, str]) -> str:
    """Execute a sub-task with context from completed dependencies."""
    dep_context = "\n".join(
        f"Result of sub-task {dep_id}: {context[dep_id]}"
        for dep_id in sub_task.depends_on
        if dep_id in context
    )

    prompt = sub_task.instruction
    if dep_context:
        prompt = f"Context from previous steps:\n{dep_context}\n\nNow: {sub_task.instruction}"
    if sub_task.output_format:
        prompt += f"\nOutput format: {sub_task.output_format}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

def run_decomposed_meta_agent(complex_task: str) -> str:
    # Step 1: Decompose
    sub_tasks, synthesis_instruction = decompose_task(complex_task)
    print(f"[meta] Decomposed into {len(sub_tasks)} sub-tasks")
    for st in sub_tasks:
        print(f"  [{st.id}] {st.instruction[:60]}... (depends on: {st.depends_on})")

    # Step 2: Execute in dependency order
    completed: dict[int, str] = {}
    for sub_task in sub_tasks:
        result = execute_sub_task(sub_task, completed)
        completed[sub_task.id] = result
        print(f"[meta] Sub-task {sub_task.id} done: {result[:50]}...")

    # Step 3: Synthesize
    all_results = "\n".join(f"Step {k}: {v[:200]}" for k, v in sorted(completed.items()))
    synthesis_prompt = f"Original task: {complex_task}\n\n{all_results}\n\n{synthesis_instruction}"

    final = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": synthesis_prompt}]
    ).content[0].text

    return final

result = run_decomposed_meta_agent(
    "Analyze the pros and cons of React vs Vue for a startup, "
    "considering developer ecosystem, learning curve, and long-term maintainability"
)
print(f"\nFinal result:\n{result[:400]}...")

# Expected Token Savings: ~-20% (decomposition adds 1 call; structured sub-tasks prevent confused multi-part single calls)
# Environment: Complex research, analysis, and planning tasks; agents handling multi-part user requests
```

---

## Option 6: Prompt Caching with Versioned Dynamic Prompts

Generate prompts dynamically then cache them by task signature to avoid regenerating identical prompts.

```python
import anthropic
import json
import hashlib
import sqlite3
from pathlib import Path

client = anthropic.Anthropic()
PROMPT_CACHE_DB = Path("/tmp/meta_prompt_cache.db")

def init_prompt_cache() -> sqlite3.Connection:
    conn = sqlite3.connect(PROMPT_CACHE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_cache (
            task_signature TEXT PRIMARY KEY,
            system_prompt TEXT NOT NULL,
            hit_count INTEGER DEFAULT 0,
            quality_score REAL DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            last_used TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

def task_signature(task_description: str, user_level: str = "general") -> str:
    """Create a stable hash for a task type + user level combination."""
    key = f"{task_description.lower().strip()}::{user_level}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]

def get_or_generate_prompt(
    conn: sqlite3.Connection,
    task_description: str,
    user_level: str = "general"
) -> tuple[str, bool]:
    """Returns (prompt, was_cached)."""
    sig = task_signature(task_description, user_level)
    row = conn.execute(
        "SELECT system_prompt FROM prompt_cache WHERE task_signature=?", (sig,)
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE prompt_cache SET hit_count=hit_count+1, last_used=datetime('now') WHERE task_signature=?",
            (sig,)
        )
        conn.commit()
        return row[0], True

    # Generate new prompt
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=f"""Generate an optimal system prompt for an AI assistant.
Task type: {task_description}
User expertise level: {user_level}
Write only the system prompt (< 150 words). No labels or commentary.""",
        messages=[{"role": "user", "content": "Generate the system prompt now."}]
    )
    prompt = response.content[0].text.strip()

    conn.execute(
        "INSERT INTO prompt_cache (task_signature, system_prompt) VALUES (?, ?)",
        (sig, prompt)
    )
    conn.commit()
    print(f"[meta-cache] Generated and cached new prompt for: {task_description[:40]}")
    return prompt, False

def rate_prompt_quality(conn: sqlite3.Connection, sig: str, score: float):
    """Update the quality score for a cached prompt based on user feedback or eval."""
    conn.execute(
        "UPDATE prompt_cache SET quality_score=? WHERE task_signature=?", (score, sig)
    )
    conn.commit()

conn = init_prompt_cache()

def run_cached_meta_prompt_agent(query: str, task_type: str, user_level: str = "general") -> str:
    prompt, was_cached = get_or_generate_prompt(conn, task_type, user_level)
    cache_status = "HIT" if was_cached else "MISS"
    print(f"[meta-cache] Cache {cache_status} for task_type={task_type[:30]}, level={user_level}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=prompt,
        messages=[{"role": "user", "content": query}]
    )
    return response.content[0].text

# First call generates and caches
print("=== First call (cache miss) ===")
run_cached_meta_prompt_agent(
    "Explain what a binary search tree is",
    task_type="explain computer science concepts",
    user_level="beginner"
)

# Second call hits cache
print("\n=== Second call (cache hit) ===")
run_cached_meta_prompt_agent(
    "What is a hash table?",
    task_type="explain computer science concepts",
    user_level="beginner"
)

# Show cache stats
rows = conn.execute("SELECT task_signature, hit_count, created_at FROM prompt_cache").fetchall()
print(f"\n[meta-cache] Cache entries: {len(rows)}")
for sig, hits, created in rows:
    print(f"  {sig}: {hits} hits, created {created}")

# Expected Token Savings: ~40% on repeated task types (prompt generation skipped on cache hit)
# Environment: High-volume production agents with recurring task patterns; multi-user platforms
```

---

## Comparison

| Option | Generation Strategy | Caching | Adapts to User | Best For |
|--------|-------------------|---------|----------------|----------|
| 1. Prompt Generator | Task classification → prompt | No | No | General-purpose diverse tasks |
| 2. Few-Shot Synthesis | Generate input/output examples | No | No | Data transformation, classification |
| 3. Prompt Optimizer | Eval against test cases + iterate | No | No | Dev-time optimization, CI/CD |
| 4. Adaptive Persona | Expertise detection → persona | No | Yes | Educational tools, developer docs |
| 5. Task Decomposer | Break into ordered sub-prompts | No | No | Complex multi-part analysis tasks |
| 6. Cached Meta-Prompts | Generate once, cache by signature | Yes | Yes | High-volume recurring task types |
