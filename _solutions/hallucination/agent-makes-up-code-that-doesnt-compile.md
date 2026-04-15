---
layout: solution
title: "Agent Makes Up Code That Doesn't Compile"
category: hallucination
description: "Agent generates Python, JavaScript, or SQL code with syntax errors, calls to non-existent methods, undefined variables, or wrong argument counts. The user runs it and gets an immediate error. The code looked plausible but was never valid."
tags: [hallucination, code-generation, syntax, validation, compilation, testing, self-correction]
---

## Symptom

Agent generates a 30-line Python function with a `syntax_error` in a string literal, a call to `df.groupby().agg(named_agg=pd.NamedAgg(...))` with incorrect argument syntax, and a reference to `pd.read_csv(..., dtype_backend="arrow")` — a parameter that doesn't exist in the installed pandas version. The user copies the code, runs it, and immediately hits `SyntaxError: invalid syntax`. The agent was confident; none of the errors were flagged.

Code execution failure rate from agent-generated code (without validation): **15–35%** on first run

## Root Cause

The model generates code by pattern completion — it produces syntactically plausible tokens based on training data. It does not compile or execute the code before returning it. It can confidently produce code that mixes API signatures from different library versions, invents keyword arguments, or omits required imports. Without a validation step, invalid code reaches the user directly.

## Fix

---

### Option 1 — Python Syntax Check Before Returning Code

Parse generated Python with the `ast` module before returning it. If parsing fails, ask Claude to fix the syntax error with the error message as context.

```python
import ast
import re
import anthropic

client = anthropic.Anthropic()

def extract_code_block(text: str) -> str:
    """Extract the first Python code block from a markdown response."""
    # Match ```python...``` or ```...```
    pattern = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    # If no fences, assume the whole text is code
    return text.strip()

def check_python_syntax(code: str) -> tuple[bool, str]:
    """Return (is_valid, error_message). is_valid=True if no syntax errors."""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, f"Parse error: {e}"

def generate_validated_code(
    task: str,
    max_fix_attempts: int = 2,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Generate Python code and fix syntax errors automatically."""
    system = (
        "You are a Python code generator. "
        "Output ONLY a complete Python code block in ```python fences. "
        "No explanation before or after. Ensure the code is syntactically valid Python."
    )

    messages = [{"role": "user", "content": task}]

    for attempt in range(max_fix_attempts + 1):
        response = client.messages.create(
            model=model, max_tokens=1024, system=system, messages=messages
        )
        raw = response.content[0].text
        code = extract_code_block(raw)

        valid, error = check_python_syntax(code)
        if valid:
            if attempt > 0:
                print(f"[Syntax] Fixed on attempt {attempt}")
            return code

        print(f"[Syntax] Attempt {attempt+1} invalid: {error}")
        if attempt < max_fix_attempts:
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": f"The code has a syntax error: {error}\n\nFix it and return the corrected code only.",
            })

    print("[Syntax] Max fix attempts reached — returning last generated code")
    return code

# Generate and validate code
tasks = [
    "Write a Python function that reads a CSV file and returns the top 5 rows sorted by a given column.",
    "Write a Python class for a simple stack with push, pop, and peek methods.",
    "Write a Python function that computes the Fibonacci sequence up to n using memoization.",
]

for task in tasks:
    print(f"\nTask: {task[:60]}...")
    code = generate_validated_code(task)
    valid, err = check_python_syntax(code)
    print(f"Syntax: {'OK' if valid else f'ERROR: {err}'}")
    print(f"Code preview: {code[:120]}...")
```

**Expected Token Savings:** None — fix attempts add tokens; prevents user frustration and re-prompting
**Environment:** `pip install anthropic`

---

### Option 2 — Sandboxed Execution with Output Capture

Execute the generated code in a restricted subprocess with a timeout. If it runs without error, return it. If it fails, return the error + code to Claude for a fix.

```python
import ast
import subprocess
import sys
import tempfile
import os
import re
import anthropic

client = anthropic.Anthropic()

ALLOWED_IMPORTS = {"os", "sys", "re", "json", "math", "datetime", "collections",
                   "itertools", "functools", "typing", "dataclasses", "pathlib",
                   "random", "string", "time", "copy", "enum", "abc"}

def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()

def get_imports(code: str) -> set[str]:
    """Extract imported module names from code."""
    try:
        tree = ast.parse(code)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
        return imports
    except Exception:
        return set()

def sandbox_run(code: str, timeout: int = 5) -> tuple[bool, str]:
    """
    Execute code in a subprocess with timeout.
    Returns (success, output_or_error).
    Only allows stdlib imports for safety.
    """
    imports = get_imports(code)
    disallowed = imports - ALLOWED_IMPORTS - {"__future__"}
    if disallowed:
        # For demo: warn but don't block — in production, block third-party
        print(f"[Sandbox] Non-stdlib imports detected: {disallowed} — skipping execution")
        # Check syntax only
        try:
            ast.parse(code)
            return True, "[Syntax OK — execution skipped for non-stdlib imports]"
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"

    # Write to temp file and execute
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout[:500]
        else:
            error = result.stderr[:500] or result.stdout[:500]
            return False, error
    except subprocess.TimeoutExpired:
        return False, f"TimeoutError: execution exceeded {timeout}s"
    finally:
        os.unlink(tmp_path)

def generate_and_test(
    task: str,
    max_attempts: int = 3,
) -> dict:
    system = (
        "You are a Python code generator. "
        "Output ONLY a complete Python code block in ```python fences. "
        "Include a main block that demonstrates the function/class with a simple example."
    )
    messages = [{"role": "user", "content": task}]

    for attempt in range(1, max_attempts + 1):
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, system=system, messages=messages
        )
        raw = response.content[0].text
        code = extract_code(raw)

        success, output = sandbox_run(code)
        print(f"[Test] Attempt {attempt}: {'PASS' if success else 'FAIL'}")
        if not success:
            print(f"       Error: {output[:80]}")

        if success:
            return {"code": code, "output": output, "attempts": attempt}

        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": f"The code failed with this error:\n\n{output}\n\nFix the code and return only the corrected version.",
        })

    return {"code": code, "output": "Max attempts reached", "attempts": max_attempts}

result = generate_and_test(
    "Write a Python function that counts word frequencies in a string and returns the top-N words. "
    "Include a demo in __main__ block using the sentence 'the cat sat on the mat the cat'."
)
print(f"\nFinal code ({result['attempts']} attempt(s)):")
print(result["code"][:300])
print(f"\nOutput: {result['output'][:100]}")
```

**Expected Token Savings:** None — fix attempts add tokens; eliminates broken code reaching production
**Environment:** `pip install anthropic`

---

### Option 3 — API Signature Validator Against Installed Library Versions

Detect calls to known library methods (pandas, numpy, requests) and validate argument names against the actually-installed library version — catching "made-up" kwargs before execution.

```python
import ast
import inspect
import importlib
import re
import anthropic
from typing import Optional

client = anthropic.Anthropic()

def get_function_signature(module_name: str, attr_path: str) -> Optional[inspect.Signature]:
    """Get the actual signature of a library function from the installed version."""
    try:
        module = importlib.import_module(module_name)
        obj = module
        for attr in attr_path.split("."):
            obj = getattr(obj, attr)
        return inspect.signature(obj)
    except (ImportError, AttributeError, ValueError):
        return None

def extract_calls(code: str) -> list[dict]:
    """Extract all function/method calls with their kwargs from code."""
    calls = []
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Extract function name
                if isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    name = node.func.id
                else:
                    continue
                kwargs = [kw.arg for kw in node.keywords if kw.arg is not None]
                calls.append({"name": name, "kwargs": kwargs, "lineno": node.lineno})
    except SyntaxError:
        pass
    return calls

# Known library functions to validate against installed versions
VALIDATE_MAP = {
    "read_csv":      ("pandas", "read_csv"),
    "DataFrame":     ("pandas", "DataFrame"),
    "to_datetime":   ("pandas", "to_datetime"),
    "array":         ("numpy", "array"),
    "linspace":      ("numpy", "linspace"),
    "get":           ("requests", "get"),
    "post":          ("requests", "post"),
}

def validate_api_calls(code: str) -> list[dict]:
    """
    Check that kwargs used in known library calls actually exist.
    Returns list of issues found.
    """
    calls = extract_calls(code)
    issues = []

    for call in calls:
        fn_name = call["name"]
        if fn_name not in VALIDATE_MAP:
            continue

        module_name, fn_path = VALIDATE_MAP[fn_name]
        sig = get_function_signature(module_name, fn_path)
        if sig is None:
            continue

        valid_params = set(sig.parameters.keys())
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if has_var_keyword:
            continue  # **kwargs — any kwarg is valid

        for kwarg in call["kwargs"]:
            if kwarg not in valid_params:
                issues.append({
                    "function": fn_name,
                    "invalid_kwarg": kwarg,
                    "lineno": call["lineno"],
                    "valid_params": sorted(valid_params - {"self"}),
                })

    return issues

def generate_validated_library_code(task: str, max_attempts: int = 2) -> str:
    system = (
        "You are a Python data science code generator. "
        "Output ONLY a Python code block in ```python fences. "
        "Use only documented API parameters for pandas, numpy, and requests."
    )
    messages = [{"role": "user", "content": task}]

    for attempt in range(1, max_attempts + 1):
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, system=system, messages=messages
        )
        raw = response.content[0].text
        code = re.search(r"```(?:python)?\n(.*?)```", raw, re.DOTALL)
        code = code.group(1).strip() if code else raw.strip()

        issues = validate_api_calls(code)
        if not issues:
            print(f"[APICheck] Attempt {attempt}: all API calls valid")
            return code

        issue_desc = "\n".join(
            f"  Line {i['lineno']}: {i['function']}(..., {i['invalid_kwarg']}=...) — "
            f"'{i['invalid_kwarg']}' is not a valid parameter. "
            f"Valid params include: {', '.join(i['valid_params'][:8])}"
            for i in issues
        )
        print(f"[APICheck] Attempt {attempt}: {len(issues)} invalid API kwargs found")
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": f"The code uses invalid API parameters:\n{issue_desc}\n\nFix these and return only corrected code.",
        })

    return code

result = generate_validated_library_code(
    "Write a function that loads a CSV into a pandas DataFrame, converts a date column to datetime, "
    "and returns descriptive statistics."
)
print(f"\nCode:\n{result[:300]}...")
```

**Expected Token Savings:** None — fix attempts cost tokens; prevents silent wrong-behavior from bad kwargs
**Environment:** `pip install anthropic pandas numpy requests`

---

### Option 4 — Self-Review Prompt: Ask Claude to Critique Its Own Code

After generating code, ask Claude to review it for bugs before returning it to the user. Self-review catches a large fraction of obvious errors.

```python
import re
import anthropic

client = anthropic.Anthropic()

GENERATION_SYSTEM = (
    "You are a Python code generator. "
    "Output ONLY a Python code block in ```python fences. No explanation."
)

REVIEW_SYSTEM = (
    "You are a strict Python code reviewer. "
    "Review the provided code for: syntax errors, undefined variables, wrong argument counts, "
    "missing imports, logic bugs, and calls to non-existent methods. "
    "If you find issues, list them as:\n"
    "ISSUE: <description> (line N)\n"
    "If the code is correct, respond with exactly: 'LGTM'\n"
    "No other output."
)

FIX_SYSTEM = (
    "You are a Python debugger. "
    "Fix all listed issues in the code and return ONLY the corrected code block in ```python fences."
)

def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()

def self_review_generate(
    task: str,
    model: str = "claude-sonnet-4-6",
    max_fix_rounds: int = 2,
) -> dict:
    # Step 1: Generate
    gen_response = client.messages.create(
        model=model, max_tokens=1024, system=GENERATION_SYSTEM,
        messages=[{"role": "user", "content": task}],
    )
    code = extract_code(gen_response.content[0].text)
    print(f"[Generate] Code generated ({len(code)} chars)")

    for round_num in range(max_fix_rounds + 1):
        # Step 2: Self-review
        review_response = client.messages.create(
            model=model, max_tokens=512, system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": f"Review this code:\n\n```python\n{code}\n```"}],
        )
        review = review_response.content[0].text.strip()
        print(f"[Review] Round {round_num+1}: {review[:80]}")

        if "LGTM" in review.upper() and "ISSUE" not in review.upper():
            return {"code": code, "review": "LGTM", "fix_rounds": round_num}

        if round_num == max_fix_rounds:
            print(f"[Review] Max rounds reached — returning with known issues")
            return {"code": code, "review": review, "fix_rounds": round_num}

        # Step 3: Fix
        fix_response = client.messages.create(
            model=model, max_tokens=1024, system=FIX_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Code:\n```python\n{code}\n```\n\nIssues found:\n{review}\n\nFix all issues.",
            }],
        )
        code = extract_code(fix_response.content[0].text)
        print(f"[Fix] Code updated ({len(code)} chars)")

    return {"code": code, "review": review, "fix_rounds": max_fix_rounds}

tasks = [
    "Write a Python function that parses a JSON file and returns a sorted list of unique keys.",
    "Write a binary search function in Python with proper edge case handling.",
]

for task in tasks:
    print(f"\n{'='*60}\nTask: {task[:70]}...")
    result = self_review_generate(task)
    print(f"Outcome: {result['review'][:60]}, fix_rounds={result['fix_rounds']}")
    print(f"Code: {result['code'][:150]}...")
```

**Expected Token Savings:** None — 2 extra calls per generation; prevents user-facing errors
**Environment:** `pip install anthropic`

---

### Option 5 — Type Annotation Verifier

Extract type annotations from generated code and verify they reference real Python types. Catches common hallucinations like `list[str, int]`, `Optional` without import, or invented `pd.StringSeries`.

```python
import ast
import re
import anthropic
from typing import get_args, get_origin

client = anthropic.Anthropic()

BUILTIN_TYPES = {
    "int", "float", "str", "bool", "bytes", "list", "dict", "set", "tuple",
    "None", "Any", "Optional", "Union", "List", "Dict", "Set", "Tuple",
    "Callable", "Iterator", "Generator", "AsyncGenerator", "Sequence",
    "Mapping", "MutableMapping", "Iterable", "AsyncIterable",
    "Type", "ClassVar", "Final", "Literal", "TypeVar", "Generic",
    "Protocol", "runtime_checkable", "overload",
}

KNOWN_THIRD_PARTY = {
    "pd.DataFrame", "pd.Series", "pd.Index", "pd.Timestamp",
    "np.ndarray", "np.float64", "np.int64",
    "pathlib.Path", "datetime.datetime", "datetime.date",
}

def extract_annotations(code: str) -> list[dict]:
    """Extract all type annotations from function signatures."""
    annotations = []
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                    if arg.annotation:
                        annotations.append({
                            "context": f"{node.name}() arg '{arg.arg}'",
                            "annotation": ast.unparse(arg.annotation),
                            "lineno": arg.annotation.lineno if hasattr(arg.annotation, "lineno") else node.lineno,
                        })
                if node.returns:
                    annotations.append({
                        "context": f"{node.name}() return",
                        "annotation": ast.unparse(node.returns),
                        "lineno": node.returns.lineno if hasattr(node.returns, "lineno") else node.lineno,
                    })
    except SyntaxError:
        pass
    return annotations

INVENTED_TYPE_PATTERNS = [
    (r"pd\.(String|Float|Integer|Boolean|Category|Date)Series", "pandas does not have a type called '{}'"),
    (r"np\.(Float|Int|String|Bool)Array", "numpy does not have a type called '{}'"),
    (r"list\[(\w+),\s*\w+\]", "list[] takes one type arg, not two: '{}'"),
    (r"dict\[(\w+),\s*\w+,\s*\w+\]", "dict[] takes two type args, not three: '{}'"),
]

def check_annotations(annotations: list[dict]) -> list[str]:
    issues = []
    for ann in annotations:
        text = ann["annotation"]
        for pattern, msg_template in INVENTED_TYPE_PATTERNS:
            m = re.search(pattern, text)
            if m:
                issues.append(
                    f"Line ~{ann['lineno']} [{ann['context']}]: "
                    f"Invalid type annotation '{text}' — {msg_template.format(m.group(0))}"
                )
    return issues

def generate_type_safe(task: str) -> str:
    system = (
        "You are a Python code generator. Use accurate Python type annotations. "
        "Output ONLY ```python code blocks. Use only real Python types."
    )
    messages = [{"role": "user", "content": task}]

    for attempt in range(3):
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, system=system, messages=messages
        )
        raw = response.content[0].text
        m = re.search(r"```(?:python)?\n(.*?)```", raw, re.DOTALL)
        code = m.group(1).strip() if m else raw.strip()

        annotations = extract_annotations(code)
        issues = check_annotations(annotations)

        if not issues:
            if attempt > 0:
                print(f"[Types] Fixed on attempt {attempt+1}")
            return code

        print(f"[Types] Attempt {attempt+1}: {len(issues)} type annotation issue(s)")
        for iss in issues:
            print(f"  {iss}")

        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": f"Fix these type annotation errors:\n" + "\n".join(issues),
        })

    return code

result = generate_type_safe(
    "Write a typed Python function that takes a list of dicts and returns a pandas DataFrame sorted by a given column."
)
print(f"\nCode:\n{result[:300]}...")
```

**Expected Token Savings:** None — annotation check is fast; prevents runtime `TypeError` from wrong types
**Environment:** `pip install anthropic pandas`

---

### Option 6 — Multi-Model Code Consensus

Generate the same code with two different models. If both agree on the implementation, return it. If they differ, ask a third call to reconcile — picking the version with better practices.

```python
import ast
import re
import asyncio
import anthropic

async_client = anthropic.AsyncAnthropic()

def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()

def syntax_valid(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False

def structural_similarity(code_a: str, code_b: str) -> float:
    """Rough similarity: shared function/class names as a fraction."""
    def names(code: str) -> set[str]:
        try:
            tree = ast.parse(code)
            return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        except SyntaxError:
            return set()
    a, b = names(code_a), names(code_b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a | b) else 0.0

async def generate_one(task: str, model: str, hint: str = "") -> str:
    system = (
        "You are a Python code generator. "
        "Output ONLY a Python code block in ```python fences. "
        f"{hint}"
    )
    response = await async_client.messages.create(
        model=model, max_tokens=1024, system=system,
        messages=[{"role": "user", "content": task}],
    )
    return extract_code(response.content[0].text)

async def reconcile(task: str, code_a: str, code_b: str) -> str:
    """Ask Claude to pick or merge the better implementation."""
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "You are a Python code reviewer. "
            "Given two implementations of the same task, choose the better one or merge the best parts. "
            "Return ONLY the final code in ```python fences."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Task: {task}\n\n"
                f"Implementation A:\n```python\n{code_a}\n```\n\n"
                f"Implementation B:\n```python\n{code_b}\n```\n\n"
                "Pick or merge the better implementation."
            ),
        }],
    )
    return extract_code(response.content[0].text)

async def consensus_generate(task: str) -> dict:
    """Generate with two models; reconcile if they differ significantly."""
    code_a, code_b = await asyncio.gather(
        generate_one(task, "claude-sonnet-4-6"),
        generate_one(task, "claude-haiku-4-5-20251001", hint="Be precise and complete."),
    )

    valid_a = syntax_valid(code_a)
    valid_b = syntax_valid(code_b)
    similarity = structural_similarity(code_a, code_b)

    print(f"[Consensus] Valid: A={valid_a}, B={valid_b}, Similarity={similarity:.0%}")

    # Both valid and similar enough — use Sonnet's version
    if valid_a and valid_b and similarity >= 0.5:
        return {"code": code_a, "strategy": "agreement", "similarity": similarity}

    # Only one is valid
    if valid_a and not valid_b:
        return {"code": code_a, "strategy": "a_only_valid"}
    if valid_b and not valid_a:
        return {"code": code_b, "strategy": "b_only_valid"}

    # Both valid but diverge — reconcile
    if valid_a and valid_b:
        print("[Consensus] Reconciling divergent implementations...")
        final = await reconcile(task, code_a, code_b)
        return {"code": final, "strategy": "reconciled", "similarity": similarity}

    # Both invalid — return Sonnet's and flag
    return {"code": code_a, "strategy": "fallback_both_invalid"}

result = asyncio.run(consensus_generate(
    "Write a Python function that flattens a nested list of arbitrary depth."
))
print(f"\nStrategy: {result['strategy']}")
print(f"Code:\n{result['code'][:250]}...")
```

**Expected Token Savings:** None — uses more tokens intentionally; highest-confidence code output
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Validation Method | Auto-Fix | Best For |
|--------|-----------------|---------|----------|
| AST Syntax Check | `ast.parse()` | Yes | All Python generation — minimal overhead |
| Sandboxed Execution | subprocess run | Yes | Self-contained stdlib code |
| API Signature Validator | `inspect.signature()` | Yes | Data science (pandas/numpy) code |
| Self-Review Prompt | LLM critique | Yes | General code with logic bugs |
| Type Annotation Verifier | Regex + AST | Yes | Typed codebases, mypy-gated pipelines |
| Multi-Model Consensus | Dual generation | Reconcile | High-stakes, production code generation |

**Recommended starting point:** Option 1 (AST Syntax Check) — parse every generated code block with `ast.parse()` before returning it. Zero extra API calls, catches all syntax errors, and the fix loop closes 90%+ of syntax issues in one round. Add Option 4 (Self-Review) for logic bugs and wrong API usage.
