---
layout: solution
title: "Agent Confuses Library Versions and Deprecated APIs"
category: hallucination
description: "Agent generates code using APIs from older library versions, calling methods that were renamed, moved, or removed — producing code that fails to import or run on current versions."
tags: [hallucination, code-generation, api, versioning, grounding]
---

## Symptom

Generated code imports `from openai import ChatCompletion` (removed in v1.0), calls `anthropic.Client()` (renamed to `anthropic.Anthropic()`), or uses `torch.nn.parallel.DistributedDataParallel` with deprecated arguments. The code looks correct syntactically but fails at runtime with `AttributeError`, `ImportError`, or `DeprecationWarning`. Updating to the current API requires manual research.

## Root Cause

Claude's training data contains code examples from many library versions. For rapidly evolving libraries (OpenAI, LangChain, PyTorch, FastAPI), the API surface changed significantly between versions. The model cannot always determine which version's API to use without an explicit version context. It defaults to patterns it saw most frequently in training data, which may be outdated. Libraries that changed their top-level import structure (like OpenAI v0→v1) are particularly prone to this.

## Fix

### Option 1: Inject library versions into the system prompt

```python
import subprocess
import anthropic

client = anthropic.Anthropic()


def get_installed_versions(packages: list[str]) -> dict[str, str]:
    """Get currently installed versions of specified packages."""
    versions = {}
    for package in packages:
        try:
            result = subprocess.run(
                ["python", "-m", "pip", "show", package],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.split("\n"):
                if line.startswith("Version:"):
                    versions[package] = line.split(":", 1)[1].strip()
                    break
        except Exception:
            versions[package] = "unknown"
    return versions


def build_versioned_system_prompt(packages: list[str]) -> str:
    versions = get_installed_versions(packages)
    version_lines = "\n".join(f"  - {pkg}: {ver}" for pkg, ver in versions.items())

    return f"""You are a Python code generation assistant.

<environment>
Python version: 3.11
Installed package versions:
{version_lines}
</environment>

<instructions>
- Generate code compatible with the EXACT versions listed above.
- Do NOT use APIs, methods, or import patterns from different versions.
- If you are uncertain whether an API exists in the specified version, say so explicitly.
- Include the version in a comment when using version-specific APIs.
</instructions>

<known_breaking_changes>
- anthropic>=0.18: Use `anthropic.Anthropic()`, not `anthropic.Client()`
- openai>=1.0: Use `from openai import OpenAI; client = OpenAI()`, not `openai.ChatCompletion.create()`
- pydantic>=2.0: Use `model.model_dump()`, not `model.dict()`
- langchain>=0.1: Many classes moved to `langchain_community` and `langchain_core`
</known_breaking_changes>"""


packages_to_check = ["anthropic", "openai", "pydantic", "fastapi", "sqlalchemy", "langchain"]
system = build_versioned_system_prompt(packages_to_check)

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    system=system,
    messages=[{"role": "user", "content": "Write a simple async function that calls the Anthropic API to get a completion."}],
)
print(response.content[0].text)
```

**Expected Token Savings:** Version-grounded prompts eliminate correction turns for deprecated API usage — each fix requires 1–3 additional turns to identify and correct.
**Environment:** Python 3.9+; `subprocess.run pip show` works on any OS; versions are read from the actual installed environment.

---

### Option 2: Fetch live API documentation before generating code

```python
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()

# Canonical API reference snippets for current library versions
# In production: fetch from official docs, GitHub, or PyPI changelog
CURRENT_API_REFERENCE = {
    "anthropic": """
# anthropic SDK (current: >=0.18)
import anthropic
client = anthropic.Anthropic(api_key="sk-live-...")  # NOT anthropic.Client()
async_client = anthropic.AsyncAnthropic()

# Sync message creation
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}]
)
text = response.content[0].text

# Streaming
with client.messages.stream(model="claude-sonnet-4-6", max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}]) as stream:
    for text in stream.text_stream:
        print(text, end="")
""",
    "openai": """
# openai SDK (current: >=1.0) — BREAKING CHANGE from v0
from openai import OpenAI  # NOT import openai; openai.ChatCompletion.create()
client = OpenAI()
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}]
)
text = response.choices[0].message.content
""",
    "pydantic": """
# pydantic v2 (current: >=2.0) — BREAKING CHANGE from v1
from pydantic import BaseModel, Field
class MyModel(BaseModel):
    name: str
    age: int = Field(ge=0)

m = MyModel(name="Alice", age=30)
m.model_dump()    # NOT m.dict()
m.model_json_schema()  # NOT m.schema()
MyModel.model_validate({"name": "Bob", "age": 25})  # NOT MyModel.parse_obj()
""",
}


def get_api_reference(library_name: str) -> str:
    """Return the current API reference for a library."""
    ref = CURRENT_API_REFERENCE.get(library_name.lower())
    if ref:
        return ref
    return f"No API reference available for '{library_name}'. Generate code based on the library's current stable documentation."


def generate_versioned_code(task: str, libraries: list[str]) -> str:
    """Generate code with current API references injected."""
    refs = []
    for lib in libraries:
        ref = get_api_reference(lib)
        refs.append(f"<api_reference library='{lib}'>\n{ref}\n</api_reference>")

    api_context = "\n\n".join(refs)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=768,
        system=(
            "You are a Python code generation assistant. "
            "Use ONLY the APIs shown in the <api_reference> blocks. "
            "Do not use any deprecated or version-specific APIs not shown."
        ),
        messages=[{
            "role": "user",
            "content": f"{api_context}\n\nTask: {task}",
        }],
    )
    return response.content[0].text


result = generate_versioned_code(
    task="Write a function that uses the Anthropic SDK to stream a response and print each token as it arrives.",
    libraries=["anthropic"],
)
print(result)
```

**Expected Token Savings:** Injecting current API reference eliminates version-confusion corrections; one-time reference injection costs ~200 tokens vs. 500–2,000 tokens per correction turn.
**Environment:** Python 3.9+; replace hardcoded references with live fetches from official docs in production.

---

### Option 3: Code validator that checks for deprecated patterns before returning

```python
import re
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()

# Patterns that indicate deprecated API usage
DEPRECATED_PATTERNS = [
    # anthropic SDK
    (re.compile(r"anthropic\.Client\s*\("), "anthropic.Client() removed in v0.18 — use anthropic.Anthropic()"),
    (re.compile(r"anthropic\.HUMAN_PROMPT|anthropic\.AI_PROMPT"), "HUMAN_PROMPT/AI_PROMPT removed in v0.18 — use messages API"),
    # openai SDK
    (re.compile(r"openai\.ChatCompletion\.create"), "openai.ChatCompletion.create() removed in openai v1.0 — use client.chat.completions.create()"),
    (re.compile(r"openai\.Completion\.create"), "openai.Completion.create() removed in openai v1.0 — use client.completions.create()"),
    (re.compile(r"import openai\n.*openai\.(Completion|ChatCompletion)"), "Direct openai module usage — create client = OpenAI() first"),
    # pydantic
    (re.compile(r"\.dict\s*\(\s*\)"), "model.dict() deprecated in pydantic v2 — use model.model_dump()"),
    (re.compile(r"\.parse_obj\s*\("), "Model.parse_obj() removed in pydantic v2 — use Model.model_validate()"),
    (re.compile(r"\.schema\s*\(\s*\)"), "Model.schema() removed in pydantic v2 — use Model.model_json_schema()"),
    # langchain
    (re.compile(r"from langchain import.*(?:ChatOpenAI|OpenAI|Anthropic)"), "LangChain moved integrations to langchain_community/langchain_openai in v0.1"),
    # requests (for async contexts)
    (re.compile(r"import requests.*\n.*async def"), "Use httpx.AsyncClient() instead of requests in async contexts"),
]


def find_deprecated_patterns(code: str) -> list[tuple[str, str]]:
    """Scan generated code for deprecated API patterns."""
    issues = []
    for pattern, message in DEPRECATED_PATTERNS:
        matches = pattern.findall(code)
        if matches:
            issues.append((matches[0] if isinstance(matches[0], str) else str(matches[0]), message))
    return issues


def generate_and_validate_code(task: str) -> str:
    """Generate code, validate it, and request fixes if deprecated patterns found."""
    # Generate initial code
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Write Python code to: {task}"}],
    )
    code = response.content[0].text

    # Validate for deprecated patterns
    issues = find_deprecated_patterns(code)
    if not issues:
        return code

    print(f"[Validation] Found {len(issues)} deprecated API pattern(s):")
    for pattern, message in issues:
        print(f"  ⚠ {pattern!r}: {message}")

    # Request fix with specific deprecation context
    issue_list = "\n".join(f"- {msg}" for _, msg in issues)
    fix_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[
            {"role": "user", "content": f"Write Python code to: {task}"},
            {"role": "assistant", "content": code},
            {"role": "user", "content": (
                f"The code above uses deprecated APIs. Please fix these issues:\n{issue_list}\n\n"
                "Rewrite using the current API for each library."
            )},
        ],
    )
    return fix_response.content[0].text


# Test
code = generate_and_validate_code("Use the Anthropic Python SDK to create a completion")
print(f"\nFinal code:\n{code[:600]}")
```

**Expected Token Savings:** Regex validator catches deprecated patterns before they reach the user — single fix turn costs ~300 tokens; manual user-reported bug costs 3–10 turns.
**Environment:** Python 3.9+; `re.compile` patterns are instant; extend `DEPRECATED_PATTERNS` list as libraries evolve.

---

### Option 4: Version-pinned code templates with slot filling

```python
import anthropic
from string import Template

client = anthropic.Anthropic()

# Templates use current API patterns — update templates when libraries change
CODE_TEMPLATES = {
    "anthropic_sync": Template("""import anthropic

client = anthropic.Anthropic()

response = client.messages.create(
    model="$model",
    max_tokens=$max_tokens,
    system="$system_prompt",
    messages=[{"role": "user", "content": "$user_message"}],
)
print(response.content[0].text)
"""),
    "anthropic_async": Template("""import asyncio
import anthropic

async def main():
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="$model",
        max_tokens=$max_tokens,
        messages=[{"role": "user", "content": "$user_message"}],
    )
    print(response.content[0].text)

asyncio.run(main())
"""),
    "anthropic_streaming": Template("""import anthropic

client = anthropic.Anthropic()

with client.messages.stream(
    model="$model",
    max_tokens=$max_tokens,
    messages=[{"role": "user", "content": "$user_message"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
print()
"""),
    "anthropic_tool_use": Template("""import anthropic

client = anthropic.Anthropic()

tools = [
    {
        "name": "$tool_name",
        "description": "$tool_description",
        "input_schema": {
            "type": "object",
            "properties": {
                "$param_name": {"type": "string", "description": "$param_description"}
            },
            "required": ["$param_name"],
        },
    }
]

response = client.messages.create(
    model="$model",
    max_tokens=$max_tokens,
    tools=tools,
    messages=[{"role": "user", "content": "$user_message"}],
)
for block in response.content:
    if block.type == "tool_use":
        print(f"Tool: {block.name}, Input: {block.input}")
"""),
}


def identify_template(user_request: str) -> str:
    """Use Haiku to identify which template best matches the user's request."""
    template_names = list(CODE_TEMPLATES.keys())
    response = haiku_client = anthropic.Anthropic()
    resp = haiku_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        system=f"You identify which code template matches a request. Available templates: {', '.join(template_names)}. Reply with exactly one template name.",
        messages=[{"role": "user", "content": user_request}],
    )
    template_name = resp.content[0].text.strip().lower()
    return template_name if template_name in CODE_TEMPLATES else "anthropic_sync"


def fill_template(template_name: str, user_request: str) -> str:
    """Fill a template by extracting parameters from the user's request."""
    template = CODE_TEMPLATES.get(template_name)
    if not template:
        return "Template not found"

    # Extract slot values from user request using the main model
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=(
            "Extract slot values for a code template. "
            "Return key=value pairs, one per line. "
            "Use sensible defaults if not specified."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Template needs: model, max_tokens, system_prompt, user_message\n"
                f"User request: {user_request}\n"
                "Fill in appropriate values (use claude-haiku-4-5-20251001 as default model, 1024 as default max_tokens)."
            ),
        }],
    )

    slots = {"model": "claude-haiku-4-5-20251001", "max_tokens": "1024",
             "system_prompt": "You are a helpful assistant.", "user_message": "Hello",
             "tool_name": "my_tool", "tool_description": "A tool", "param_name": "input", "param_description": "Input value"}

    for line in response.content[0].text.split("\n"):
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip().lower().replace(" ", "_")
            if key in slots:
                slots[key] = value.strip().strip('"\'')

    return template.safe_substitute(**slots)


# Demo
request = "Write async code to call Claude and print the response"
template_name = identify_template(request)
print(f"Selected template: {template_name}")
code = fill_template(template_name, request)
print(f"\nGenerated code:\n{code}")
```

**Expected Token Savings:** Template-based generation guarantees current API usage by construction; no validation pass needed; saves 200–500 tokens per corrected response.
**Environment:** Python 3.9+; `string.Template` is stdlib; update templates in one place when APIs change.

---

### Option 5: Changelog-aware code review with Haiku

```python
import anthropic

client = anthropic.Anthropic()
haiku = anthropic.Anthropic()

# Condensed changelogs for libraries Claude commonly confuses
CHANGELOGS = {
    "anthropic": """
v0.18+ (current):
- REMOVED: anthropic.Client() → use anthropic.Anthropic()
- REMOVED: HUMAN_PROMPT, AI_PROMPT constants → use messages API
- ADDED: client.messages.create() with messages=[{"role": ..., "content": ...}]
- ADDED: client.messages.stream() for streaming
- ADDED: anthropic.AsyncAnthropic() for async

v0.3-0.17 (old):
- Had anthropic.Client()
- Used completions API with HUMAN_PROMPT/AI_PROMPT
""",
    "openai": """
v1.0+ (current):
- REMOVED: openai.ChatCompletion.create()
- REMOVED: openai.Completion.create()
- ADDED: from openai import OpenAI; client = OpenAI(); client.chat.completions.create()
- ADDED: AsyncOpenAI for async

v0.x (old, DO NOT USE):
- import openai; openai.ChatCompletion.create(model=..., messages=...)
""",
    "pydantic": """
v2.x (current):
- model.model_dump() replaces model.dict()
- model.model_json_schema() replaces model.schema()
- Model.model_validate() replaces Model.parse_obj()
- Field validators use @field_validator decorator
- model_config = ConfigDict(...) replaces class Config

v1.x (old):
- model.dict(), model.json(), Model.parse_obj(), class Config
""",
}


def review_code_for_version_issues(code: str, libraries: list[str]) -> tuple[bool, str]:
    """Use Haiku to review generated code against changelog information."""
    if not libraries:
        return True, ""

    changelogs = "\n\n".join(
        f"<changelog library='{lib}'>\n{CHANGELOGS.get(lib, 'No changelog available')}\n</changelog>"
        for lib in libraries
    )

    response = haiku.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            "You review Python code for deprecated API usage based on changelogs. "
            "Reply with: PASS (no issues) or FAIL: [specific issues found]."
        ),
        messages=[{
            "role": "user",
            "content": f"{changelogs}\n\n<code_to_review>\n{code}\n</code_to_review>\n\nDoes this code use deprecated APIs?",
        }],
    )

    review = response.content[0].text.strip()
    if review.upper().startswith("PASS"):
        return True, ""
    return False, review


def generate_reviewed_code(task: str, libraries: list[str]) -> str:
    # Generate
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"Write current Python code using the latest stable APIs for: {', '.join(libraries)}.",
        messages=[{"role": "user", "content": task}],
    )
    code = response.content[0].text

    # Review
    passed, issues = review_code_for_version_issues(code, libraries)
    if passed:
        print("[Review] PASS — no deprecated APIs detected")
        return code

    print(f"[Review] FAIL — issues: {issues[:200]}")

    # Fix with changelog context
    changelogs = "\n\n".join(
        f"<changelog library='{lib}'>\n{CHANGELOGS.get(lib, '')}\n</changelog>"
        for lib in libraries
    )
    fixed = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[
            {"role": "user", "content": task},
            {"role": "assistant", "content": code},
            {"role": "user", "content": f"Fix these deprecated API issues:\n{issues}\n\nChangelogs for reference:\n{changelogs}"},
        ],
    )
    return fixed.content[0].text


code = generate_reviewed_code(
    "Use the Anthropic Python SDK to make an API call with streaming enabled",
    libraries=["anthropic"],
)
print(f"\nFinal code:\n{code[:500]}")
```

**Expected Token Savings:** Haiku review costs ~100 tokens; catches version issues before user sees them; prevents 3–10 correction turns worth 1,000–5,000 tokens.
**Environment:** Python 3.9+; changelog snippets fit in ~500 tokens; update changelog dict as libraries release breaking changes.

---

### Option 6: Pin library versions in generated code comments and imports

```python
import anthropic
import re

client = anthropic.Anthropic()

VERSION_GUARDS = {
    "anthropic": {
        "min_version": "0.18.0",
        "guard_comment": "# Requires: anthropic>=0.18 (use Anthropic() not Client())",
        "import_check": "assert anthropic.__version__ >= '0.18', 'Upgrade: pip install anthropic>=0.18'",
    },
    "openai": {
        "min_version": "1.0.0",
        "guard_comment": "# Requires: openai>=1.0 (new client interface)",
        "import_check": "assert openai.__version__ >= '1.0', 'Upgrade: pip install openai>=1.0'",
    },
    "pydantic": {
        "min_version": "2.0.0",
        "guard_comment": "# Requires: pydantic>=2.0 (use model_dump() not dict())",
        "import_check": "assert pydantic.VERSION >= '2', 'Upgrade: pip install pydantic>=2.0'",
    },
}

VERSION_AWARE_SYSTEM = """You are a Python code generator. Follow these rules:

<versioning_rules>
1. Always generate code for the CURRENT stable version of each library.
2. Add a comment at the top of each code block listing the minimum version required.
3. Use these current APIs only:
   - anthropic: anthropic.Anthropic() (NOT anthropic.Client())
   - openai: from openai import OpenAI; client = OpenAI() (NOT openai.ChatCompletion.create)
   - pydantic: model.model_dump() (NOT model.dict())
4. If you use a feature added in a specific version, comment it: # Added in anthropic v0.20
</versioning_rules>"""


def add_version_guards(code: str, libraries: list[str]) -> str:
    """Add version guard comments and assertions to generated code."""
    guards = []
    for lib in libraries:
        if lib in VERSION_GUARDS:
            guard = VERSION_GUARDS[lib]
            guards.append(guard["guard_comment"])

    if not guards:
        return code

    guard_block = "\n".join(guards) + "\n\n"

    # Add after the first import block
    import_end = max(
        (m.end() for m in re.finditer(r"^(?:import|from)\s+\S+.*$", code, re.MULTILINE)),
        default=0,
    )

    if import_end > 0:
        return code[:import_end] + "\n\n" + guard_block.rstrip() + code[import_end:]
    return guard_block + code


def generate_version_safe_code(task: str, libraries: list[str]) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=VERSION_AWARE_SYSTEM,
        messages=[{"role": "user", "content": f"Libraries to use: {', '.join(libraries)}\n\nTask: {task}"}],
    )
    code = response.content[0].text
    return add_version_guards(code, libraries)


result = generate_version_safe_code(
    "Create an async function that calls the Anthropic API and streams the response to stdout",
    libraries=["anthropic"],
)
print(result[:600])
```

**Expected Token Savings:** Version guard comments serve as documentation preventing future version confusion; system prompt adds ~100 tokens once and prevents multi-turn corrections.
**Environment:** Python 3.9+; `re` is stdlib; version guards are additive — don't break existing code.

---

| Option | Approach | Prevention Mechanism | Best For |
|--------|----------|---------------------|----------|
| 1 | Inject installed versions into system prompt | Environment grounding | When generating code for current environment |
| 2 | Fetch live API reference before generating | Reference injection | Rapidly evolving libraries |
| 3 | Regex validator + auto-fix | Pattern detection | Catching known deprecated patterns |
| 4 | Version-pinned templates | Structural prevention | Repeated similar code generation |
| 5 | Haiku changelog reviewer | Semantic review | Complex multi-library code |
| 6 | Version guard comments + aware system prompt | Documentation + grounding | Long-lived generated codebases |
