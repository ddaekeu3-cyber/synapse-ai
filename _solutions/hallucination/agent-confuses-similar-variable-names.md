---
layout: solution
title: "Agent Confuses Similar Variable Names — Uses Wrong Variable in Code"
category: hallucination
description: "Agent generates code mixing up user_id and user_input, token and access_token, result and results. Variables with similar names get swapped. Code runs but produces wrong output or crashes."
tags: [hallucination, variable-names, code-generation, naming, scope, bugs]
---

# Agent Confuses Similar Variable Names — Uses Wrong Variable in Code

## Symptom
- Generated code uses `user_id` where `user_input` was intended — wrong type error
- `token` and `access_token` swapped — auth fails silently with wrong value
- `result` used where `results` (list) expected — `AttributeError: 'str' has no attribute 'append'`
- Agent correctly declares variables but uses wrong one in the implementation
- Bug only appears at runtime, not obvious from reading the generated code

## Root Cause
The model predicts likely token sequences. When multiple similar variable names are in scope, the model may pick the statistically more likely one rather than the contextually correct one. This is especially common with: singular/plural confusion (`result`/`results`), scope confusion (local vs. parameter), and common naming patterns (`id`, `token`, `data`, `result`).

## Fix

### Option 1: Use distinctive, unambiguous variable names

```
System prompt / coding instruction:
"Variable naming rules to avoid confusion:
- Never use 'result' and 'results' in the same scope — use 'item' and 'items'
- Qualify ambiguous names: 'user_api_token' not 'token'
- Avoid single-letter variables except loop counters (i, j, k)
- Use typed names: 'user_id_str', 'count_int', 'user_list'
- If two variables have similar purposes, make names maximally different:
  GOOD: 'raw_response' and 'parsed_result'
  BAD: 'response' and 'response_data'"
```

### Option 2: Type hints to catch confusion at generation time

```python
# With explicit types, the model has stronger signal about what each variable holds
from typing import Optional
import anthropic

# BAD — ambiguous names, easy to confuse
def process(token, result, id):
    ...

# GOOD — distinctive names with type hints
def process_user_request(
    auth_token: str,
    query_result: dict,
    user_id: int
) -> anthropic.Message:
    # Now the model (and type checker) can catch variable swaps
    ...
```

### Option 3: Request code review focused on variable usage

```
After generating code, ask:
"Review the generated code specifically for variable name confusion:
1. List every variable name in scope simultaneously
2. For each variable usage, confirm it matches the correct declared variable
3. Check especially: singular vs plural, prefixed vs unprefixed versions
4. List any variables with similar names that could be confused"
```

### Option 4: Generate with explicit variable registry

```python
# When asking agent to generate complex code, provide a variable registry

prompt = """
Generate the data processing function.

Variable registry (use EXACTLY these names):
- raw_api_response: dict — the HTTP response from the API (do not confuse with parsed_records)
- parsed_records: list[dict] — the parsed list of records (do not confuse with raw_api_response)
- current_user_id: int — the logged-in user's ID (do not confuse with record_owner_id)
- record_owner_id: int — the ID of the record's creator

The function should filter parsed_records where record_owner_id == current_user_id.
"""
```

### Option 5: Static analysis to catch variable confusion

```python
import ast, sys

def check_variable_confusion(source_code: str) -> list[str]:
    """Detect potentially confused similar variable names"""
    tree = ast.parse(source_code)
    issues = []

    class VariableCollector(ast.NodeVisitor):
        def __init__(self):
            self.assigned = set()
            self.used = set()

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Store):
                self.assigned.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                self.used.add(node.id)

    collector = VariableCollector()
    collector.visit(tree)

    # Find similar variable pairs
    all_vars = collector.assigned | collector.used
    for v1 in all_vars:
        for v2 in all_vars:
            if v1 >= v2:
                continue
            # Check for singular/plural confusion
            if v1 + "s" == v2 or v2 + "s" == v1:
                issues.append(f"Possible confusion: '{v1}' vs '{v2}' (singular/plural)")
            # Check for prefix confusion
            if v1 in v2 and len(v2) - len(v1) <= 3:
                issues.append(f"Possible confusion: '{v1}' vs '{v2}' (one is prefix of other)")

    return issues

# After agent generates code:
generated_code = agent.generate(prompt)
issues = check_variable_confusion(generated_code)
if issues:
    print("Potential variable name confusion detected:")
    for issue in issues:
        print(f"  - {issue}")
```

### Option 6: Rename problematic variables before passing to agent

```python
def prepare_context_for_agent(codebase_context: str) -> str:
    """Rename ambiguous variables before agent sees them"""
    # If codebase has confusing names, rename for the agent's benefit
    renames = {
        "result": "query_result",
        "token": "auth_bearer_token",
        "data": "raw_payload_data",
        "id": "database_record_id",
    }

    note = "Variable names in this codebase:\n"
    for old, new in renames.items():
        note += f"  - '{old}' is referred to as '{new}' for clarity\n"
        codebase_context = codebase_context.replace(old, new)

    return note + "\n" + codebase_context
```

## High-Risk Variable Name Pairs

| Confused pair | Risk level | Better naming |
|---|---|---|
| `result` / `results` | High | `query_result` / `query_results` |
| `token` / `access_token` / `refresh_token` | High | Fully qualified always |
| `id` / `user_id` / `record_id` | High | Always fully qualified |
| `data` / `raw_data` / `parsed_data` | Medium | Add stage prefix |
| `response` / `api_response` | Medium | Always add source prefix |
| `config` / `user_config` / `default_config` | Medium | Scope-qualify |
| `i` / `index` | Low | Use `i` only for simple loops |

## Expected Token Savings
Debugging wrong-variable bugs in generated code: ~5,000 tokens
Clear naming conventions prevent most cases: 0 wasted

## Environment
- Any code-generating agent; most common with complex functions and many variables
- Source: direct experience, common code generation failure pattern
