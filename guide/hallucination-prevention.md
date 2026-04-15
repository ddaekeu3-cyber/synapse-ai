---
layout: default
title: "AI Agent Hallucination Prevention — Detection, Verification, and Recovery"
description: "Practical guide to preventing, detecting, and recovering from AI agent hallucinations. Covers output validation, grounding techniques, temperature settings, and verification pipelines."
permalink: /guide/hallucination-prevention
---

# AI Agent Hallucination Prevention Guide

Hallucinations in production AI agents aren't just wrong answers — they're wrong answers that get acted on. An agent that fabricates an API endpoint, invents a library function, or confidently misremembers a previous decision can corrupt data, break systems, or produce outputs that cost more to fix than to redo from scratch.

---

## Types of Hallucination in Agent Systems

| Type | Example | Risk Level |
|------|---------|-----------|
| Factual fabrication | Invents a function that doesn't exist | High |
| Decision confabulation | "You asked me to do X" when X was never asked | High |
| Specification drift | Subtly changes task requirements mid-execution | Medium |
| Confident uncertainty | States unknown things with certainty | Medium |
| Source invention | Cites non-existent documentation or issues | Low |

---

## Fix 1: Temperature Calibration by Task Type

Temperature is the single biggest lever for hallucination control.

```yaml
# openclaw.config.yaml
providers:
  anthropic:
    temperature_by_task:
      factual_lookup: 0.0       # Deterministic for facts
      code_generation: 0.1      # Low for code
      analysis: 0.3             # Some creativity for analysis
      creative_writing: 0.8     # High for creative tasks
```

For agent tasks that involve facts, code, or precise instructions: **temperature ≤ 0.2**.

---

## Fix 2: Verification Pipeline

The most reliable hallucination prevention — verify outputs before acting on them:

```python
async def verified_agent_action(task):
    # Step 1: Generate
    result = await agent.execute(task)

    # Step 2: Verify code (if output contains code)
    if contains_code(result):
        test_result = await sandbox.run(result.code)
        if test_result.error:
            # Re-generate with error context
            result = await agent.execute(
                task,
                context=f"Previous attempt failed: {test_result.error}"
            )

    # Step 3: Verify facts (if output makes factual claims)
    if contains_factual_claims(result):
        for claim in extract_claims(result):
            if not verify_against_sources(claim):
                result.flag_uncertain(claim)

    return result
```

---

## Fix 3: Grounding — Force Citation

Agents hallucinate less when required to cite sources for every claim:

```
System prompt addition:
"For every factual claim you make, cite the specific file, line number,
or documentation source. If you cannot cite a source, say
'I believe X but cannot verify — please confirm before acting.'"
```

This forces the agent to distinguish between what it knows from context vs. what it's generating.

---

## Fix 4: Explicit Uncertainty Expression

Train the agent to express uncertainty rather than guess confidently:

```
System prompt:
"Use these phrases when uncertain:
- 'I believe [X] — please verify before acting'
- 'I'm not certain about [X] — I recommend checking [source]'
- 'I cannot confirm [X] with the context available'

Never state something as fact unless it appears in the provided context,
codebase, or tool output."
```

---

## Fix 5: Decision Provenance Logging

Hallucinations in multi-step tasks often compound — an early fabrication becomes a "fact" in later steps. Prevent this with provenance:

```yaml
agent:
  decision_logging:
    enabled: true
    log_path: ./decisions.md
    format: |
      ## Decision: {description}
      **Source:** {source}  # file:line, tool output, user instruction, or "model inference"
      **Confidence:** {high|medium|low}
      **Verified:** {yes|no|pending}
```

When source is "model inference" and confidence is low, flag for human review before acting.

---

## Fix 6: Hallucination Detection Patterns

Common patterns that indicate a hallucination:

```python
HALLUCINATION_SIGNALS = [
    # Function/method doesn't exist in codebase
    r'\.(\w+)\(\)',          # Call a method → verify it exists
    # Specific numbers stated without source
    r'\b\d{4,}\b',           # Large specific numbers → ask for source
    # "I remember" claims
    r'(you said|you asked|earlier you|previously you)',
    # Invented citations
    r'(according to|as stated in|per the docs)',
]

def flag_for_review(response):
    for pattern in HALLUCINATION_SIGNALS:
        if re.search(pattern, response, re.IGNORECASE):
            return True  # Needs verification
    return False
```

---

## Fix 7: Sandboxed Code Verification

For agents that generate and execute code — always execute in sandbox first:

```yaml
sandbox:
  enabled: true
  pre_execution_check: true
  timeout_ms: 10000
  on_error:
    action: report_and_retry
    max_retries: 2
    provide_error_to_agent: true
```

Never execute agent-generated code directly in production. Test in sandbox, check output, then apply if correct.

---

## Recovering from a Hallucination Incident

When you discover an agent acted on hallucinated information:

1. **Stop the agent** — don't let it continue building on the false foundation
2. **Identify the scope** — what decisions were made after the hallucination?
3. **Check for side effects** — were any external systems, files, or APIs modified?
4. **Restart with correction** — provide the correct information explicitly in the new session prompt
5. **Add it to your verification checklist** — what check would have caught this?

---

## Common Questions

### How do I tell if an agent output is hallucinated?

Check if the claim is verifiable from the context the agent had access to. If the agent says "function X exists in file Y" — verify it. If the agent says "you previously asked for Z" — check the conversation history. Anything specific and unverifiable deserves a second look.

### Should I use RAG to prevent hallucinations?

Retrieval-Augmented Generation helps with factual grounding but doesn't prevent all hallucinations. The agent can still misinterpret retrieved documents or generate incorrect code. RAG + verification pipeline is more robust than RAG alone.

### How much does hallucination prevention cost in tokens?

A verification pipeline adds roughly 20–40% overhead. But a single hallucination caught early is worth more than that — fixing a corrupted database or wrong deployment costs 10–100x more tokens than the verification would have.

---

[← View all hallucination solutions](/synapse-ai/solutions/hallucination/)

**Related guides:**
- [Context Window Errors](/synapse-ai/guide/context-window-errors) — truncation can cause hallucinations
- [Loop / Stuck Errors](/synapse-ai/guide/loop-stuck-errors) — hallucination-driven loops
- [Token Saving Guide](/synapse-ai/guide/token-saving) — verification is cheaper than correction

<div class="cta-box">
  <h3>Catch hallucinations before they cause damage</h3>
  <p>SynapseAI includes verification patterns and hallucination detection for common agent output types.</p>
  <code>clawhub install synapse-ai</code>
</div>
