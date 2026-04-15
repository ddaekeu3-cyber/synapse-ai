---
layout: solution
title: "Agent Invents API Endpoints That Don't Exist"
category: hallucination
description: "Agent confidently calls URLs it fabricated from partial knowledge, returning 404s or hitting unrelated resources."
tags: [hallucination, tool-failure, validation, api, grounding]
---

## Symptom

Your agent constructs HTTP requests to endpoints like `/v2/users/search/advanced` or `/api/orders/bulk_cancel` that don't exist in the actual API. The call returns a 404 or an unexpected response, but the agent either ignores the error, retries with another invented URL, or confidently reports success based on the fabricated endpoint name.

## Root Cause

Language models are trained on API documentation, Stack Overflow, and blog posts. They pattern-match endpoint shapes from memory rather than from an authoritative source. When the documented API differs from training data — or the model is asked about a service it only partially knows — it extrapolates plausible-sounding paths that may never have existed.

## Fix

### Option 1 — Allowlist of valid endpoints validated before each call

```python
import httpx
import anthropic

client = anthropic.Anthropic()

# Authoritative list of valid endpoints loaded from your OpenAPI spec or hardcoded
VALID_ENDPOINTS: set[str] = {
    "/v1/users",
    "/v1/users/{id}",
    "/v1/orders",
    "/v1/orders/{id}",
    "/v1/products",
    "/v1/products/{id}",
    "/v1/search",
    "/v1/webhooks",
}

def normalise_path(path: str) -> str:
    """Replace numeric/UUID segments with {id} placeholder."""
    import re
    path = re.sub(r"/\d+", "/{id}", path)
    path = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/{id}", path)
    return path

def safe_api_call(method: str, path: str, base_url: str = "https://api.example.com") -> dict:
    normalised = normalise_path(path)
    if normalised not in VALID_ENDPOINTS:
        raise ValueError(
            f"Invalid endpoint: {path!r} (normalised: {normalised!r}). "
            f"Valid endpoints: {sorted(VALID_ENDPOINTS)}"
        )
    with httpx.Client(timeout=10.0) as http:
        resp = http.request(method, f"{base_url}{path}")
        resp.raise_for_status()
        return resp.json()

# Agent grounded by tool schema that only exposes valid paths
TOOLS = [
    {
        "name": "call_api",
        "description": "Call a known API endpoint. Only the listed paths are valid.",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
                "path": {
                    "type": "string",
                    "description": "One of: " + ", ".join(sorted(VALID_ENDPOINTS)),
                },
            },
            "required": ["method", "path"],
        },
    }
]

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=512,
    system="You are an API assistant. Only call endpoints listed in the tool description.",
    tools=TOOLS,
    messages=[{"role": "user", "content": "Get all orders."}],
)
print(response.content)
```

**Expected Token Savings:** Prevents multi-turn retry loops caused by 404 responses; each blocked call saves ~300–600 tokens.
**Environment:** Agents with a known, finite set of API endpoints; best when you control the API surface.

---

### Option 2 — Load valid routes from OpenAPI spec at startup

```python
import json
import re
import httpx
import anthropic

client = anthropic.Anthropic()

def load_openapi_routes(spec_url: str) -> set[str]:
    """Fetch OpenAPI spec and extract all valid path+method combinations."""
    with httpx.Client(timeout=10.0) as http:
        spec = http.get(spec_url).json()

    routes: set[str] = set()
    for path, methods in spec.get("paths", {}).items():
        for method in methods:
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                routes.add(f"{method.upper()} {path}")
    return routes

def validate_route(method: str, path: str, valid_routes: set[str]) -> bool:
    """Check if method+path matches a known route (with {param} wildcards)."""
    normalised = re.sub(r"/[^/]+", lambda m: "/{id}" if re.match(r"^/[0-9a-f-]+$|^/\d+$", m.group()) else m.group(), path)
    candidate = f"{method.upper()} {normalised}"
    if candidate in valid_routes:
        return True
    # Try parameter wildcard matching
    for route in valid_routes:
        pattern = re.escape(route).replace(r"\{[^}]+\}", "[^/]+")
        if re.fullmatch(pattern.replace(r"\{", "{").replace(r"\}", "}").replace("{[^/]+}", "[^/]+"),
                        f"{method.upper()} {path}"):
            return True
    return False

# Example: load from a public petstore spec
try:
    VALID_ROUTES = load_openapi_routes("https://petstore3.swagger.io/api/v3/openapi.json")
    print(f"Loaded {len(VALID_ROUTES)} valid routes from OpenAPI spec")
except Exception:
    VALID_ROUTES = {"GET /pet/{petId}", "POST /pet", "GET /store/inventory"}
    print("Using fallback route list")

def grounded_request(method: str, path: str) -> str:
    if not validate_route(method, path, VALID_ROUTES):
        return f"ERROR: {method} {path} is not a documented endpoint."
    return f"OK: would call {method} {path}"

# Test grounding
print(grounded_request("GET",  "/pet/123"))           # valid
print(grounded_request("GET",  "/pet/search/fuzzy"))  # likely invalid
print(grounded_request("POST", "/store/inventory"))   # likely invalid
```

**Expected Token Savings:** Automatically stays in sync with the API spec; no manual allowlist maintenance needed.
**Environment:** Agents targeting third-party APIs with published OpenAPI specs; run at agent startup, cache for session.

---

### Option 3 — Pre-call URL probe (HEAD request)

```python
import httpx
import anthropic

client = anthropic.Anthropic()

def probe_endpoint(url: str, timeout: float = 3.0) -> bool:
    """Send a HEAD request to verify the endpoint exists before the real call."""
    try:
        with httpx.Client(timeout=timeout) as http:
            resp = http.head(url)
            # 405 Method Not Allowed still means the path exists
            return resp.status_code not in {404, 410}
    except httpx.RequestError:
        return False

def safe_get(url: str) -> dict | str:
    if not probe_endpoint(url):
        return f"Endpoint does not exist: {url}"
    with httpx.Client(timeout=10.0) as http:
        resp = http.get(url)
        if resp.status_code == 200:
            return resp.json()
        return f"HTTP {resp.status_code}: {resp.text[:200]}"

# Demonstrate
urls_to_try = [
    "https://httpbin.org/get",           # real
    "https://httpbin.org/invented/path", # fake
    "https://httpbin.org/status/200",    # real
]
for url in urls_to_try:
    result = safe_get(url)
    if isinstance(result, str):
        print(f"BLOCKED: {result}")
    else:
        print(f"OK: {list(result.keys())}")
```

**Expected Token Savings:** Stops the agent from sending a full request body to a 404 endpoint; prevents downstream tool_result parsing errors.
**Environment:** Public REST APIs where HEAD requests are cheap; do not use for rate-limited or mutation endpoints.

---

### Option 4 — Grounding via RAG over API documentation

```python
import anthropic

client = anthropic.Anthropic()

# Simplified: embed your OpenAPI paths as a searchable reference in the system prompt
API_REFERENCE = """
Available API endpoints (complete list):
GET    /v1/users               — list all users
GET    /v1/users/{id}          — get user by ID
POST   /v1/users               — create user
PUT    /v1/users/{id}          — update user
DELETE /v1/users/{id}          — delete user
GET    /v1/orders              — list orders
GET    /v1/orders/{id}         — get order by ID
POST   /v1/orders              — create order
GET    /v1/products            — list products
GET    /v1/products/{id}       — get product by ID
GET    /v1/search?q=...        — full-text search

Rules:
- ONLY call endpoints listed above.
- If the user asks for something that requires an endpoint not listed, say so explicitly.
- Never invent or guess endpoint paths.
"""

def ask_about_api(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=API_REFERENCE,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# Grounded responses
print(ask_about_api("How do I get a list of all users?"))
print(ask_about_api("How do I bulk delete orders? (say if impossible)"))
print(ask_about_api("Can I search products by category?"))
```

**Expected Token Savings:** Eliminates hallucinated endpoints before any HTTP call is made; system prompt caching makes the reference nearly free after the first call.
**Environment:** Agents that discuss or suggest API calls before executing them; pairs with prompt caching for large API docs.

---

### Option 5 — Self-review: agent checks its own endpoint before calling

```python
import re
import json
import anthropic

client = anthropic.Anthropic()

VALID_PATHS = [
    "/v1/users", "/v1/users/{id}", "/v1/orders", "/v1/orders/{id}",
    "/v1/products", "/v1/search",
]

REVIEW_SYSTEM = f"""You are an API validator. Given an HTTP method and path, decide if it is valid.
Valid paths: {json.dumps(VALID_PATHS)}
Respond with exactly one JSON object: {{"valid": true/false, "reason": "..."}}
"""

def self_review_endpoint(method: str, path: str) -> tuple[bool, str]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=REVIEW_SYSTEM,
        messages=[{"role": "user", "content": f"{method} {path}"}],
    )
    try:
        data = json.loads(resp.content[0].text)
        return bool(data["valid"]), str(data.get("reason", ""))
    except (json.JSONDecodeError, KeyError):
        return False, "review failed to parse"

def safe_call(method: str, path: str) -> str:
    valid, reason = self_review_endpoint(method, path)
    if not valid:
        return f"BLOCKED — {reason}"
    return f"OK — executing {method} {path}"

candidates = [
    ("GET",  "/v1/users"),
    ("GET",  "/v1/users/bulk/export"),   # invented
    ("POST", "/v1/orders"),
    ("GET",  "/v1/analytics/funnel"),    # invented
]
for method, path in candidates:
    print(f"{method} {path}: {safe_call(method, path)}")
```

**Expected Token Savings:** A Haiku self-review costs ~100 tokens and prevents a wasted tool call + error handling turn.
**Environment:** Agents where the list of valid endpoints is too dynamic for a static allowlist; adds one cheap validation step.

---

### Option 6 — Structured tool schema as the only endpoint source of truth

```python
import json
import anthropic

client = anthropic.Anthropic()

# Each tool is exactly one endpoint — the model cannot invent paths outside the schema
TOOLS = [
    {
        "name": "list_users",
        "description": "Retrieve paginated list of users.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page":     {"type": "integer", "default": 1},
                "per_page": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "get_user",
        "description": "Retrieve a single user by their ID.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "create_order",
        "description": "Create a new order for a user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id":    {"type": "string"},
                "product_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["user_id", "product_ids"],
        },
    },
]

TOOL_HANDLERS = {
    "list_users":   lambda inp: {"users": [{"id": "u1", "name": "Alice"}], "total": 1},
    "get_user":     lambda inp: {"id": inp["user_id"], "name": "Bob", "email": "bob@example.com"},
    "create_order": lambda inp: {"order_id": "o123", "status": "pending"},
}

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output  = handler(block.input) if handler else {"error": f"unknown tool: {block.name}"}
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output)})
        messages.append({"role": "user", "content": results})

    return "max steps reached"

print(run_agent("Show me the user with ID u42 and then create an order for them."))
```

**Expected Token Savings:** Tool schema is the definitive endpoint contract; the model physically cannot call a path outside the schema.
**Environment:** The most robust option — preferred architecture for any agent that interacts with an API.

---

## Comparison

| Option | Endpoint Source | Latency Added | Dynamic? | Best For |
|---|---|---|---|---|
| 1. Allowlist | Hardcoded set | None | No | Small, stable API surfaces |
| 2. OpenAPI spec | Remote spec | Startup only | Yes | Third-party APIs with published specs |
| 3. HEAD probe | Live server | +1 HTTP call | Yes | Public APIs with free HEAD support |
| 4. RAG / system prompt | Inline docs | None | Semi | Discussion-mode agents |
| 5. Self-review | Haiku model | +1 LLM call | Semi | Dynamic path lists |
| 6. Tool schema | Schema definition | None | No | All tool-using agents (preferred) |
