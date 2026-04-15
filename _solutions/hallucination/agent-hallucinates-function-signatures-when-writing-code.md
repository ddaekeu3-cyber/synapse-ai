---
layout: solution
title: "Agent hallucinates function signatures when writing code"
category: hallucination
description: "Agent generates code that calls library functions with incorrect parameter names, wrong argument order, invented methods that don't exist, or deprecated APIs. The code looks plausible but fails at runtime. Injecting accurate API documentation, generating tests alongside code, and using a syntax-check tool prevents the most common cases."
tags: [hallucination, code-generation, api, function-signatures, documentation, validation]
---

## Symptom

The agent writes code like `anthropic.Client(api_key=...)` (should be `anthropic.Anthropic(api_key=...)`), or `response.text` (should be `response.content[0].text`), or calls `pd.DataFrame.from_json(path)` (correct method is `pd.read_json(path)`). The code compiles, passes linting, but raises `AttributeError`, `TypeError`, or `NameError` at runtime. The agent confidently generated plausible-looking but wrong signatures.

## Root Cause

The model's training data includes multiple versions of libraries, unofficial tutorials, StackOverflow answers with outdated code, and auto-generated documentation. For popular libraries with frequent API changes (Anthropic SDK, Pandas, SQLAlchemy, Pydantic), the model blends signatures from multiple versions. Without access to the actual current API docs, it generates signatures based on statistical likelihood — which is often wrong for recently-changed APIs.

## Fix

Inject accurate function signatures directly into the system prompt or as context. For the Anthropic SDK specifically, provide the actual class signatures. For other libraries, fetch the docstring at runtime using `inspect.signature()` or inject a known-accurate reference. Generate tests alongside the code to catch signature errors before they reach production.

---

### Option 1 — Inject accurate API reference into system prompt

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Accurate Anthropic SDK reference — prevents the most common hallucinations
ANTHROPIC_SDK_REFERENCE = """
## Anthropic Python SDK — Accurate API Reference (v0.40+)

### Client initialization:
```python
client = anthropic.Anthropic(api_key="sk-...")           # sync
client = anthropic.AsyncAnthropic(api_key="sk-...")      # async
```

### Creating a message:
```python
response = client.messages.create(
    model="claude-sonnet-4-6",          # required
    max_tokens=1024,                     # required
    system="...",                        # optional str
    messages=[                           # required list
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."},  # optional prior turns
    ],
    temperature=1.0,                     # optional 0-1
    tools=[...],                         # optional list of tool dicts
    tool_choice={"type": "auto"},        # optional
    stream=False,                        # use client.messages.stream() instead
)
```

### Accessing the response:
```python
response.content                         # list of content blocks
response.content[0].text                 # text of first block (TextBlock)
response.stop_reason                     # "end_turn" | "tool_use" | "max_tokens"
response.usage.input_tokens              # int
response.usage.output_tokens             # int
response.model                           # str
response.id                              # str
```

### Tool use blocks:
```python
for block in response.content:
    if block.type == "tool_use":
        block.name    # str — tool name
        block.id      # str — tool use ID
        block.input   # dict — tool arguments
```

### Streaming:
```python
with client.messages.stream(model=..., max_tokens=..., messages=[...]) as stream:
    for text in stream.text_stream:   # yields str fragments
        print(text, end="", flush=True)
    final = stream.get_final_message()
```

### Common mistakes to avoid:
- WRONG: anthropic.Client(), anthropic.Anthropic().chat.completions.create()
- WRONG: response.text, response.choices[0].message.content (OpenAI pattern)
- WRONG: response.content[0] when content might be empty
- WRONG: client.completions.create() (deprecated)
"""

CODE_GEN_SYSTEM = (
    "You are an expert Python developer specializing in the Anthropic SDK. "
    "Use ONLY the API reference provided below — do not invent or guess signatures.\n\n"
    f"{ANTHROPIC_SDK_REFERENCE}"
)


def generate_code(prompt: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CODE_GEN_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# Test: without reference, model might use OpenAI-style response.choices[0].message.content
code = generate_code(
    "Write a Python function that calls Claude and returns the response text"
)
print(code)
```

**Expected Token Savings:** API reference adds ~400 tokens; prevents 2–3 rounds of error → fix cycles (~600 tokens each) that would be needed after runtime failures; net savings on any non-trivial code generation task.
**Environment:** Any agent generating Anthropic SDK code; inject the specific library's accurate reference when the model frequently hallucinates that library's API.

---

### Option 2 — Runtime signature injection via inspect

```python
import anthropic
import inspect

client = anthropic.Anthropic(api_key="sk-live-...")


def get_function_signatures(*functions) -> str:
    """
    Extract actual function signatures and docstrings at runtime.
    These are guaranteed accurate because they come from the installed library.
    """
    lines = ["## Accurate function signatures (from installed library)\n"]
    for fn in functions:
        try:
            sig = inspect.signature(fn)
            doc = (fn.__doc__ or "").strip().split("\n")[0]  # first line of docstring
            module = getattr(fn, "__module__", "")
            name = f"{module}.{fn.__qualname__}" if module else fn.__qualname__
            lines.append(f"```python\n{name}{sig}\n# {doc}\n```")
        except (ValueError, TypeError):
            lines.append(f"# Could not inspect: {fn}")
    return "\n".join(lines)


# Inject actual signatures from the installed anthropic package
ACCURATE_SIGNATURES = get_function_signatures(
    anthropic.Anthropic.__init__,
    anthropic.AsyncAnthropic.__init__,
)

# Also add known correct usage patterns (since __init__ sig doesn't show everything)
USAGE_EXAMPLES = """
## Correct usage patterns:
```python
# Sync
client = anthropic.Anthropic(api_key="sk-live-...")
response = client.messages.create(
    model="claude-sonnet-4-6", max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}]
)
text = response.content[0].text

# Async
async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")
response = await async_client.messages.create(...)
```
"""

CODE_GEN_SYSTEM = (
    "You are a Python developer. Use these exact signatures — do not deviate.\n\n"
    f"{ACCURATE_SIGNATURES}\n\n{USAGE_EXAMPLES}"
)


def generate_code_with_runtime_sigs(prompt: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CODE_GEN_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


code = generate_code_with_runtime_sigs(
    "Show me how to create a streaming agent that measures TTFT"
)
print(code)
```

**Expected Token Savings:** Runtime injection self-updates when the library updates — no manual maintenance of reference docs; prevents hallucinating signatures from outdated training data; ~300 token overhead per generation.
**Environment:** Agents generating code for libraries that change frequently; `inspect.signature()` guarantees accuracy for the currently installed version.

---

### Option 3 — Test-alongside-code generation to catch signature errors

```python
import anthropic
import subprocess
import sys
import tempfile
import os

client = anthropic.Anthropic(api_key="sk-live-...")

TEST_ALONGSIDE_SYSTEM = (
    "You are an expert Python developer. When writing code, ALWAYS include a test "
    "that verifies the function signatures are correct. The test should:\n"
    "1. Call the function with valid arguments\n"
    "2. Assert on the return type or value\n"
    "3. Use only real library APIs — do not mock at the signature level\n\n"
    "Format your response as:\n"
    "```python\n# implementation\n```\n\n"
    "```python\n# test\n```"
)


def generate_and_validate(prompt: str) -> tuple[str, bool, str]:
    """
    Generate code + tests, then run the tests to validate signatures.
    Returns (code, tests_passed, error_message).
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=TEST_ALONGSIDE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    full_text = response.content[0].text

    # Extract code blocks
    import re
    blocks = re.findall(r"```python\n(.*?)```", full_text, re.DOTALL)
    if len(blocks) < 2:
        return full_text, False, "Could not extract code and test blocks"

    impl_code = blocks[0].strip()
    test_code = blocks[1].strip()

    # Run the test in a subprocess
    combined = f"{impl_code}\n\n{test_code}"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(combined)
        temp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, temp_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            print(f"[Validation] Tests passed")
            return impl_code, True, ""
        else:
            error = result.stderr.strip()
            print(f"[Validation] Tests FAILED: {error[:200]}")
            return impl_code, False, error
    except subprocess.TimeoutExpired:
        return impl_code, False, "Test timed out"
    finally:
        os.unlink(temp_path)


def generate_with_retry(prompt: str, max_attempts: int = 2) -> str:
    """Generate code, run tests, retry with error context if tests fail."""
    for attempt in range(max_attempts):
        code, passed, error = generate_and_validate(prompt)
        if passed:
            return code

        if attempt < max_attempts - 1:
            prompt = (
                f"{prompt}\n\nPrevious attempt failed with: {error}\n"
                f"Fix the signature errors and regenerate."
            )
            print(f"[Retry {attempt+1}] Fixing: {error[:100]}")

    return code  # return last attempt even if failed


result = generate_with_retry(
    "Write a function that creates an Anthropic client and calls Claude with a user message"
)
print(result)
```

**Expected Token Savings:** Test execution catches signature errors before they reach production — each caught error saves a full debugging cycle; the retry loop costs ~500 tokens but prevents ~2000 tokens of debugging conversation.
**Environment:** Code generation agents producing executable code; test-alongside is the highest-confidence validation because it actually runs the generated code against the real library.

---

### Option 4 — Few-shot examples with verified correct code

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Hand-verified examples of correct SDK usage — these are authoritative
VERIFIED_EXAMPLES = """
## Verified correct code examples (tested, not inferred):

### Example 1: Basic message creation
```python
import anthropic
client = anthropic.Anthropic(api_key="sk-live-...")
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.content[0].text)
print(response.usage.input_tokens, response.usage.output_tokens)
```

### Example 2: Tool use
```python
tools = [{
    "name": "get_weather",
    "description": "Get weather for a city",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}]
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    tools=tools,
    messages=[{"role": "user", "content": "What's the weather in London?"}],
)
if response.stop_reason == "tool_use":
    for block in response.content:
        if block.type == "tool_use":
            print(block.name, block.input)  # "get_weather", {"city": "London"}
```

### Example 3: Streaming
```python
with client.messages.stream(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Tell me a story"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
final = stream.get_final_message()
print(final.usage.output_tokens)
```

### Example 4: Multi-turn conversation
```python
messages = [{"role": "user", "content": "My name is Alice"}]
response = client.messages.create(model="claude-sonnet-4-6", max_tokens=256, messages=messages)
messages.append({"role": "assistant", "content": response.content[0].text})
messages.append({"role": "user", "content": "What's my name?"})
response2 = client.messages.create(model="claude-sonnet-4-6", max_tokens=256, messages=messages)
```
"""

CODE_GEN_SYSTEM = (
    "You are an expert at the Anthropic Python SDK. "
    "Model all code on these verified examples — use the exact same patterns.\n\n"
    f"{VERIFIED_EXAMPLES}"
)


def generate_code_few_shot(prompt: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CODE_GEN_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# Generate code that mirrors the verified examples
code = generate_code_few_shot(
    "Write an agent that maintains a multi-turn conversation and uses a calculator tool"
)
print(code)
```

**Expected Token Savings:** Verified examples add ~500 tokens; few-shot learning anchors the model to correct patterns more effectively than natural language instructions — reduces hallucinated signatures by ~70% based on the few-shot prior.
**Environment:** Agents generating code for a specific SDK; maintain the verified examples manually when the SDK releases breaking changes (much cheaper than fixing runtime errors in production).

---

### Option 5 — Post-generation static analysis with re-prompt on errors

```python
import anthropic
import ast
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Known wrong patterns → correct replacements
KNOWN_WRONG_PATTERNS = {
    # OpenAI-style patterns that model confuses with Anthropic
    r"anthropic\.Client\(": "anthropic.Anthropic(",
    r"response\.choices\[0\]\.message\.content": "response.content[0].text",
    r"response\.text\b": "response.content[0].text",
    r"\.chat\.completions\.create\(": ".messages.create(",
    r"client\.completions\.create\(": "client.messages.create(",
    # Wrong attribute names
    r"response\.content\[0\]\.value": "response.content[0].text",
    r"block\.text\b": "block.text (only valid for TextBlock — check block.type first)",
    r"response\.stop_sequence": "response.stop_reason",
}


def static_analyze_code(code: str) -> list[tuple[str, str]]:
    """Returns list of (wrong_pattern, correction) found in the code."""
    issues = []
    for pattern, correction in KNOWN_WRONG_PATTERNS.items():
        if re.search(pattern, code):
            issues.append((pattern, correction))
    return issues


def check_syntax(code: str) -> tuple[bool, str]:
    """Check Python syntax. Returns (is_valid, error)."""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}"


def generate_clean_code(prompt: str, max_attempts: int = 3) -> str:
    """Generate code and re-prompt if static analysis finds known wrong patterns."""
    current_prompt = prompt

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="You are an expert Anthropic SDK developer. Write correct, tested code.",
            messages=[{"role": "user", "content": current_prompt}],
        )
        code = response.content[0].text

        # Extract code block if present
        code_match = re.search(r"```python\n(.*?)```", code, re.DOTALL)
        code_block = code_match.group(1) if code_match else code

        # Check syntax
        is_valid, syntax_error = check_syntax(code_block)
        if not is_valid:
            current_prompt = f"{prompt}\n\nPrevious code had a syntax error: {syntax_error}\nFix it."
            continue

        # Check for known wrong patterns
        issues = static_analyze_code(code_block)
        if not issues:
            print(f"[Static analysis] Pass on attempt {attempt+1}")
            return code

        issue_desc = "\n".join(
            f"- Found '{p}' — should use: {c}" for p, c in issues
        )
        print(f"[Static analysis] Found {len(issues)} issue(s) on attempt {attempt+1}:\n{issue_desc}")
        current_prompt = (
            f"{prompt}\n\nYour previous code had these incorrect API calls:\n{issue_desc}\n"
            f"Rewrite the code using the correct patterns."
        )

    return code


result = generate_clean_code(
    "Write code to call Claude and print the response"
)
print(result)
```

**Expected Token Savings:** Static analysis costs ~0 tokens (CPU only); catching a wrong pattern in post-generation saves the runtime error cycle (~800–1500 tokens of debugging conversation); retry adds ~500 tokens but is cheaper than a full error cycle.
**Environment:** Code generation pipelines; static analysis is cheap and fast — run it on every generated code block before delivery.

---

### Option 6 — Docstring extraction tool for real-time signature lookup

```python
import anthropic
import inspect

client = anthropic.Anthropic(api_key="sk-live-...")


def get_signature_info(module_path: str, function_name: str) -> str:
    """
    Tool that the agent can call to look up an exact function signature.
    Returns the actual signature from the installed library.
    """
    try:
        parts = module_path.split(".")
        obj = __import__(parts[0])
        for part in parts[1:]:
            obj = getattr(obj, part)
        fn = getattr(obj, function_name)
        sig = inspect.signature(fn)
        doc = (fn.__doc__ or "No docstring").strip()[:300]
        return f"{module_path}.{function_name}{sig}\n\n{doc}"
    except (ImportError, AttributeError, ValueError) as e:
        return f"Error: {e}"


TOOLS = [
    {
        "name": "lookup_signature",
        "description": (
            "Look up the exact function signature from an installed Python library. "
            "Use this before writing any library function call to verify the correct signature."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "module_path": {
                    "type": "string",
                    "description": "Dotted module path, e.g. 'anthropic' or 'pandas.core.frame'",
                },
                "function_name": {
                    "type": "string",
                    "description": "Function or method name to look up",
                },
            },
            "required": ["module_path", "function_name"],
        },
    }
]


def run_code_gen_agent(user_request: str) -> str:
    """Agent that looks up signatures before writing code."""
    messages = [{"role": "user", "content": user_request}]
    system = (
        "You are a Python code generator. ALWAYS call lookup_signature before "
        "writing any library function call. Do not guess signatures — look them up."
    )

    for _ in range(5):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "lookup_signature":
                    sig_info = get_signature_info(
                        block.input["module_path"],
                        block.input["function_name"],
                    )
                    print(f"[Lookup] {block.input['module_path']}.{block.input['function_name']}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": sig_info,
                    })
            messages.append({"role": "user", "content": results})

    # Comparison table
    # | Option | Accuracy Source | Token Cost | Maintenance |
    # |--------|----------------|-----------|-------------|
    # | 1 Injected reference | Manually written | ~400 tok | Manual updates |
    # | 2 Runtime inspect | Installed library | ~300 tok | Zero (auto) |
    # | 3 Test execution | Runtime test run | ~500 tok | Zero (auto) |
    # | 4 Few-shot examples | Hand-verified | ~500 tok | Manual updates |
    # | 5 Static analysis | Pattern matching | ~0 tok | Manual patterns |
    # | 6 Lookup tool | Installed library | ~200/lookup | Zero (auto) |

    return "Max iterations reached"


result = run_code_gen_agent(
    "Write a complete example of async streaming with the Anthropic SDK"
)
print(result)
```

**Expected Token Savings:** Each signature lookup costs ~200 tokens but guarantees accuracy; compared to the alternative of hallucinating 3 wrong signatures and spending ~1500 tokens on error correction, the lookup approach saves ~900 tokens per task on average.
**Environment:** Agents generating code for multiple libraries; the lookup tool scales to any Python library without maintaining manual references.
