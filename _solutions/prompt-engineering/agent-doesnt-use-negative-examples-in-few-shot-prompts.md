---
layout: solution
title: "Agent Doesn't Use Negative Examples in Few-Shot Prompts"
category: prompt-engineering
description: "Agent's few-shot prompts only show correct outputs, leaving the model uncertain about what to avoid — causing the same failure modes to recur."
tags: [prompt-engineering, few-shot, negative-examples, output-quality, format-control]
---

## Symptom

Agent repeatedly produces outputs that violate constraints despite positive-only examples:

```python
# Prompt with only positive examples:
system = """Extract the product name from user reviews.

Example 1:
Review: "I love my new iPhone 15 Pro, the camera is amazing"
Output: iPhone 15 Pro

Example 2:
Review: "The Samsung Galaxy S24 broke after one week"
Output: Samsung Galaxy S24"""

# Model still produces wrong outputs:
# Review: "This phone is great but the battery dies fast"
# Output: "This phone" ← wrong: no product found, should return null
# Output: "battery" ← wrong: attribute not product name
# Output: "great phone" ← wrong: description not product name
```

The model sees what a correct extraction looks like but has no signal about what "wrong" looks like. It guesses plausible-sounding completions that match the surface pattern of the examples but violate the semantic intent.

## Root Cause

Few-shot learning is fundamentally pattern matching. Positive examples define what the model should do but leave the "no-product" case, partial-match case, and attribute-extraction case undefined. Without contrastive examples showing these failure modes and why they're wrong, the model interpolates to the nearest positive pattern rather than recognising a genuinely different situation.

## Fix

---

### Option 1: Paired Positive/Negative Examples with Explicit Rejection Reasons

Add a "wrong output + reason" block alongside each positive example so the model sees both what to do and what to avoid.

```python
import anthropic

EXTRACTION_PROMPT = """Extract the product name from the review. Return ONLY the product name, or "null" if no specific product is named.

=== EXAMPLES ===

Review: "I love my new iPhone 15 Pro, the camera is amazing"
WRONG: "camera" ← This is a product feature, not the product name
WRONG: "I love my new iPhone 15 Pro" ← Do not include surrounding words
CORRECT: iPhone 15 Pro

Review: "This is the best coffee I've ever had, highly recommend"
WRONG: "coffee" ← Too generic, not a specific named product
WRONG: "best coffee" ← This is a description, not a product name
CORRECT: null

Review: "My Sony WH-1000XM5 headphones arrived damaged"
WRONG: "Sony" ← Brand alone is not sufficient, include full model
WRONG: "Sony headphones" ← Missing the model number
CORRECT: Sony WH-1000XM5

=== TASK ===
Review: {review}
CORRECT:"""

client = anthropic.Anthropic()

def extract_product(review: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[
            {
                "role": "user",
                "content": EXTRACTION_PROMPT.format(review=review),
            }
        ],
    )
    return response.content[0].text.strip()

# Test cases that previously failed
test_reviews = [
    "This phone is great but the battery dies fast",         # no named product
    "My new MacBook Pro M3 runs everything perfectly",       # clear product
    "The device stopped working after a month",              # vague reference
    "I returned my Dyson V15 — suction was disappointing",  # product with sentiment
]

for review in test_reviews:
    result = extract_product(review)
    print(f"Review: {review!r}\nExtracted: {result}\n")
```

**Expected Token Savings:** Contrastive examples add ~200 tokens to the prompt but reduce failure-mode correction turns by 70-80%. At 3 correction turns avoided per session × 400 tokens each = 1,200 tokens saved vs 200 spent. Net: 6× return.
**Environment:** Works with any extraction task. The WRONG+reason pattern is most effective when wrong outputs are plausible-looking — the model needs explicit signal that these are incorrect.

---

### Option 2: Format Violation Gallery — Show Every Invalid Output Shape

When format constraints are critical (JSON, markdown, specific length), show a gallery of every common format violation so the model internalises all constraints simultaneously.

```python
import json
import anthropic

FORMAT_PROMPT = """Classify customer support tickets. Return a JSON object with exactly these fields:
{{"category": string, "priority": "low"|"medium"|"high", "sentiment": "positive"|"neutral"|"negative"}}

=== VALID OUTPUT ===
{{"category": "billing", "priority": "high", "sentiment": "negative"}}

=== INVALID OUTPUTS — never produce these ===

Missing fields:
{{"category": "billing", "priority": "high"}}
Why wrong: must include all 3 fields

Extra fields:
{{"category": "billing", "priority": "high", "sentiment": "negative", "confidence": 0.9}}
Why wrong: return exactly the 3 required fields, nothing more

Wrong enum value:
{{"category": "billing", "priority": "urgent", "sentiment": "negative"}}
Why wrong: priority must be low/medium/high only

Wrapped in markdown:
```json
{{"category": "billing", "priority": "high", "sentiment": "negative"}}
```
Why wrong: return raw JSON only, no code block or explanation

Sentiment mismatch:
Ticket: "I'm furious about this charge" → {{"sentiment": "neutral"}}
Why wrong: explicit anger → negative

=== CLASSIFY THIS TICKET ===
Ticket: {ticket}
Output:"""

client = anthropic.Anthropic()

def classify_ticket(ticket: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": FORMAT_PROMPT.format(ticket=ticket)}],
    )
    raw = response.content[0].text.strip()
    # Strip accidental markdown wrapping despite instructions
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)

tickets = [
    "My invoice has a duplicate charge of $89.99 and I'm very upset",
    "Quick question: how do I update my email address?",
    "Thanks for the fast resolution! Really appreciate it",
]

for ticket in tickets:
    result = classify_ticket(ticket)
    print(f"Ticket: {ticket!r}")
    print(f"Result: {result}\n")
```

**Expected Token Savings:** Format violations require post-processing retries (re-call the model + parse again). Each retry: ~600 tokens. Violation gallery adds ~400 tokens once, eliminates ~3 retries/session = saves 1,400 tokens net per session.
**Environment:** Format gallery is most valuable when the output schema has multiple constraints that can be violated independently. Test against your actual failure modes before writing the gallery.

---

### Option 3: Contrastive Chain-of-Thought — Show Correct and Incorrect Reasoning

For reasoning tasks, show not just wrong answers but wrong reasoning chains so the model learns to avoid flawed inference patterns, not just flawed conclusions.

```python
import anthropic

COT_PROMPT = """Determine if a customer is eligible for a refund based on our policy:
- Purchased within 30 days: eligible
- Item unused/unopened: eligible
- Digital products: NOT eligible (no exceptions)
- Subscription services: NOT eligible after first use

=== EXAMPLE 1 ===
Customer: "I bought a physical book 15 days ago and haven't opened it yet"

WRONG REASONING: The customer bought it 15 days ago which is recent, so they should get a refund.
WRONG ANSWER: Eligible
Why this reasoning fails: Recency alone isn't enough — must verify ALL criteria explicitly.

CORRECT REASONING:
1. Physical book → not a digital product ✓
2. Not a subscription ✓
3. 15 days ago < 30 days ✓
4. "Haven't opened it" → unused ✓
All criteria met.
CORRECT ANSWER: Eligible

=== EXAMPLE 2 ===
Customer: "I subscribed to your service last week and only used it once, I want a refund"

WRONG REASONING: Only used it once, so barely used — should qualify as "unused".
WRONG ANSWER: Eligible
Why this reasoning fails: "Subscription after first use" policy applies regardless of usage amount. One use = used.

CORRECT REASONING:
1. This is a subscription service
2. Customer explicitly states they used it ("used it once")
3. Policy: subscription NOT eligible after first use
4. One use = first use = not eligible
CORRECT ANSWER: Not eligible — subscription policy applies

=== TASK ===
Customer: {customer_message}

CORRECT REASONING:"""

client = anthropic.Anthropic()

def evaluate_refund(customer_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": COT_PROMPT.format(customer_message=customer_message)}],
    )
    return response.content[0].text

# Edge cases that positive-only prompts typically fail on
cases = [
    "I bought a digital audiobook 5 days ago but haven't listened to it — can I return it?",
    "My trial subscription ended and I forgot to cancel, I was charged once",
    "Purchased physical item 28 days ago, it's still sealed, but I lost the receipt",
]

for case in cases:
    result = evaluate_refund(case)
    print(f"Case: {case!r}\n{result}\n{'─'*60}\n")
```

**Expected Token Savings:** Contrastive CoT adds ~350 tokens per example pair. For complex policy evaluation, this eliminates escalation to human review for edge cases — saving 5-10 correction turns at ~800 tokens each = 4,000-8,000 tokens saved per ambiguous session.
**Environment:** Most valuable for multi-criteria classification, policy evaluation, and eligibility checks where wrong reasoning is common even when conclusions occasionally match.

---

### Option 4: Boundary-Condition Examples — Target the Decision Boundary Specifically

Add examples that sit exactly at the boundary of correctness, showing the model where to draw the line.

```python
import anthropic

BOUNDARY_PROMPT = """Rate the toxicity of comments: "safe", "borderline", or "toxic".

=== CLEAR SAFE (no doubt) ===
"I disagree with your analysis" → safe
"This product didn't work for me" → safe

=== CLEAR TOXIC (no doubt) ===
"[explicit slur] get out of here" → toxic
"I'll find you and make you pay" → toxic

=== BOUNDARY CASES — pay attention to these ===

"Your argument is stupid" → borderline
Reason: attacks idea/argument quality, not person; no slurs; but dismissive

"You're wrong and obviously don't understand this topic" → borderline
Reason: condescending tone and ad hominem hint, but no explicit insult

"You're an idiot" → toxic
Reason: direct personal attack, even without slurs

"That's a stupid thing to say" → borderline
Reason: attacks the statement (not person), but still dismissive

"Go back to where you came from" → toxic
Reason: xenophobic dog-whistle even without explicit slurs

"I hate people who think like you" → toxic
Reason: dehumanising generalisation targeting the person, not the idea

=== KEY DISTINCTIONS ===
- Attacks on ideas/arguments → borderline (context-dependent)
- Attacks on the person → toxic
- Condescending but non-violent → borderline
- Threats, slurs, dehumanisation → always toxic

=== RATE THIS COMMENT ===
Comment: {comment}
Rating:"""

client = anthropic.Anthropic()

def rate_toxicity(comment: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": BOUNDARY_PROMPT.format(comment=comment)}],
    )
    return response.content[0].text.strip()

# Test boundary cases
comments = [
    "This is the dumbest policy I've ever seen",
    "You clearly haven't thought this through",
    "People like you are why nothing gets done",
    "I strongly disagree with every point you made",
]

for comment in comments:
    rating = rate_toxicity(comment)
    print(f"{rating:12} | {comment!r}")
```

**Expected Token Savings:** Boundary examples reduce the "borderline treated as safe" and "borderline treated as toxic" misclassifications that require human review. Each avoided review: ~2,000 tokens of correction dialogue. Adding 6 boundary examples (~300 tokens) prevents 10-15 misclassifications per 100 comments.
**Environment:** Requires domain knowledge to identify the actual decision boundary for your task. Collect boundary examples from your existing error logs — they're the cases your model is already getting wrong.

---

### Option 5: Anti-Pattern Catalogue — Document All Known Failure Modes Upfront

Build a catalogue of every failure pattern observed in production and inject the relevant anti-patterns into the system prompt based on the task type.

```python
import anthropic

# Catalogue keyed by task type
ANTI_PATTERN_CATALOGUES: dict[str, str] = {
    "code_generation": """
ANTI-PATTERNS — never generate these:

1. Hardcoded secrets
   BAD:  api_key = "sk-live-abc123"
   GOOD: api_key = os.environ["ANTHROPIC_API_KEY"]

2. Broad exception catching without re-raise
   BAD:  except Exception: pass
   GOOD: except SpecificError as e: logger.error(e); raise

3. Mutable default arguments
   BAD:  def fn(items=[]): items.append(x)
   GOOD: def fn(items=None): items = items or []

4. String formatting with user input (injection risk)
   BAD:  query = f"SELECT * FROM users WHERE name = '{user_input}'"
   GOOD: cursor.execute("SELECT * FROM users WHERE name = %s", (user_input,))

5. Blocking I/O in async functions
   BAD:  async def fetch(): return requests.get(url)  # blocks event loop
   GOOD: async def fetch(): return await httpx.AsyncClient().get(url)
""",
    "summarisation": """
ANTI-PATTERNS — never produce these:

1. Adding information not in source
   BAD:  Source says "sales up 5%", summary says "sales up significantly"
   GOOD: Preserve exact figures from source

2. Losing the main point for interesting details
   BAD:  3-sentence summary about an anecdote, 1 sentence on main conclusion
   GOOD: Main conclusion gets proportional coverage

3. Vague hedges without basis
   BAD:  "The results seem promising although further study may be needed"
   GOOD: Only add hedges if the source itself hedges

4. First-person perspective bleed
   BAD:  "In this article, we explored..."
   GOOD: "The article explores..."
""",
}

def build_system_prompt(task_type: str, base_instruction: str) -> str:
    anti_patterns = ANTI_PATTERN_CATALOGUES.get(task_type, "")
    return f"{base_instruction}\n\n{anti_patterns}" if anti_patterns else base_instruction

client = anthropic.Anthropic()

def generate_code(task: str) -> str:
    system = build_system_prompt(
        "code_generation",
        "You are a Python expert. Write clean, production-ready code.",
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

code = generate_code(
    "Write a function that fetches user data from our API using the API key from config"
)
print(code)
```

**Expected Token Savings:** Anti-pattern catalogue adds ~300-500 tokens to system prompt (cached after first call with prompt caching). Prevents security vulnerabilities, style violations, and logic errors that each require a correction turn (~600 tokens). For 10 calls/session with 2 prevented violations each = saves 1,200 tokens after the catalogue overhead.
**Environment:** Use prompt caching (`cache_control: {"type": "ephemeral"}`) on the anti-pattern catalogue so the ~400 token overhead is only paid once per session. Update catalogue from production error logs quarterly.

---

### Option 6: Automated Negative Example Mining from Production Errors

Automatically collect real failure cases from production and synthesise them into negative examples for the next prompt version.

```python
import json
from pathlib import Path
from collections import Counter
import anthropic

# Production error log format
EXAMPLE_ERROR_LOG = [
    {"input": "Extract product: 'This phone has great battery'", "output": "battery", "expected": "null", "error_type": "attribute_extraction"},
    {"input": "Extract product: 'The device is perfect'", "output": "the device", "expected": "null", "error_type": "pronoun_extraction"},
    {"input": "Extract product: 'iPhone battery drains fast'", "output": "iPhone battery", "expected": "iPhone", "error_type": "feature_concat"},
    {"input": "Classify: 'ok i guess'", "output": "positive", "expected": "neutral", "error_type": "sentiment_bias"},
    {"input": "Classify: 'not bad'", "output": "negative", "expected": "neutral", "error_type": "negation_misread"},
]

def mine_negative_examples(error_log: list[dict], max_examples: int = 3) -> str:
    """Group errors by type and synthesise negative example blocks."""
    by_type: dict[str, list[dict]] = {}
    for err in error_log:
        by_type.setdefault(err["error_type"], []).append(err)

    # Take the most common error types
    sorted_types = sorted(by_type.items(), key=lambda x: len(x[1]), reverse=True)
    blocks = []
    for error_type, examples in sorted_types[:max_examples]:
        sample = examples[0]
        blocks.append(
            f"Common mistake ({error_type.replace('_', ' ')}):\n"
            f"  Input: {sample['input']}\n"
            f"  WRONG output: {sample['output']!r}\n"
            f"  CORRECT output: {sample['expected']!r}\n"
        )
    return "\n".join(blocks)

def synthesise_improved_prompt(base_task: str, error_log: list[dict]) -> str:
    """Use LLM to write negative examples from error log."""
    client = anthropic.Anthropic()

    error_summary = json.dumps(error_log[:5], indent=2)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You write few-shot prompt negative examples from production error logs.",
        messages=[{
            "role": "user",
            "content": (
                f"Task: {base_task}\n\n"
                f"Production errors:\n{error_summary}\n\n"
                "Write 3 WRONG/CORRECT example pairs that would have prevented these errors. "
                "Format each as:\nWRONG: <output>\nWhy wrong: <reason>\nCORRECT: <output>\n"
            ),
        }],
    )
    return response.content[0].text

# Build prompt from mined errors
mined = mine_negative_examples(EXAMPLE_ERROR_LOG)
synthesised = synthesise_improved_prompt(
    "Extract exact product names from reviews",
    EXAMPLE_ERROR_LOG,
)

final_prompt = f"""Extract the exact product name from the review. Return null if no specific product is named.

=== KNOWN MISTAKES TO AVOID ===
{mined}

=== LLM-SYNTHESISED ANTI-PATTERNS ===
{synthesised}

Review: {{review}}
Product name:"""

client = anthropic.Anthropic()

def extract_with_mined_examples(review: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": final_prompt.format(review=review)}],
    )
    return response.content[0].text.strip()

# Comparison table
"""
| Approach | Example Type | Token Cost | Failure Coverage | Maintenance |
|---|---|---|---|---|
| Option 1: Paired pos/neg | Manual contrastive | ~200 | Hand-picked modes | Low |
| Option 2: Format gallery | Format violations | ~400 | All format shapes | Low |
| Option 3: Contrastive CoT | Reasoning chains | ~350/pair | Logic errors | Medium |
| Option 4: Boundary cases | Decision boundary | ~300 | Edge cases | Medium |
| Option 5: Anti-pattern catalogue | Known bad patterns | ~400 (cached) | Domain-specific | Low |
| Option 6: Production mining | Real failures | ~500 + LLM call | Actual errors | Automated |
"""

test_reviews = [
    "The phone has excellent battery life",
    "Samsung Galaxy S24 Ultra is worth every penny",
    "This device failed after 2 weeks",
]
for r in test_reviews:
    print(f"{extract_with_mined_examples(r)!r:20} ← {r!r}")
```

**Expected Token Savings:** Automated mining creates negative examples from actual production failures, targeting the exact failure modes that cost tokens in practice. Each prevented failure avoids 1-3 correction turns (~600-1,800 tokens). With 20 calls/day and 15% failure rate, saving 3 corrections/day × 1,200 tokens = 3,600 tokens/day.
**Environment:** Schedule mining to run weekly against your error logs. Store synthesised negative examples in a versioned prompt registry. Use `claude-haiku-4-5-20251001` for synthesis to keep mining cost minimal.
