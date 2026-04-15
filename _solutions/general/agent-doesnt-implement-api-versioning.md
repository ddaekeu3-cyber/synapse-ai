---
layout: solution
title: "Agent Doesn't Implement API Versioning"
category: general
description: "Agents that expose unversioned HTTP APIs break all clients when schemas change. API versioning lets you evolve the agent's interface without forcing immediate client upgrades."
tags: [general, api-versioning, fastapi, backward-compatibility, semver, deprecation]
---

# Agent Doesn't Implement API Versioning

When an agent's HTTP API changes — new required fields, renamed parameters, different response shapes — unversioned APIs break every client simultaneously. Without versioning, you must either keep all clients in sync with every deployment or never change the schema at all. Both options are unsustainable.

## Why This Happens

Versioning feels like extra complexity for a v1 project. Developers add it "when needed" — which is after the first breaking change has already broken production clients.

---

## Option 1: URL Path Versioning

The most common pattern: `/v1/agent/run`, `/v2/agent/run`. Old versions remain available until deprecated.

```python
from fastapi import FastAPI, APIRouter
import anthropic

client = anthropic.Anthropic()
app = FastAPI(title="Synapse Agent API")

# --- v1: simple text-only request/response ---
v1_router = APIRouter(prefix="/v1")


@v1_router.post("/agent/run")
async def run_v1(prompt: str) -> dict:
    """v1: accepts plain string prompt, returns flat text result."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": response.content[0].text}


# --- v2: structured request with model selection and metadata ---
from pydantic import BaseModel


class RunRequestV2(BaseModel):
    prompt: str
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024
    system_prompt: str | None = None


class RunResponseV2(BaseModel):
    result: str
    model: str
    input_tokens: int
    output_tokens: int


v2_router = APIRouter(prefix="/v2")


@v2_router.post("/agent/run", response_model=RunResponseV2)
async def run_v2(req: RunRequestV2) -> RunResponseV2:
    """v2: structured request/response with token usage metadata."""
    kwargs = {}
    if req.system_prompt:
        kwargs["system"] = req.system_prompt

    response = client.messages.create(
        model=req.model,
        max_tokens=req.max_tokens,
        messages=[{"role": "user", "content": req.prompt}],
        **kwargs,
    )
    return RunResponseV2(
        result=response.content[0].text,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


app.include_router(v1_router)
app.include_router(v2_router)


@app.get("/")
def root():
    return {
        "versions": ["v1", "v2"],
        "current": "v2",
        "deprecated": ["v1"],
        "docs": "/docs",
    }
```

**Expected Token Savings:** Clients can migrate at their own pace; no forced simultaneous upgrades that cause rushed, buggy deployments.

**Environment:** Any HTTP API; most compatible versioning strategy for external clients.

---

## Option 2: Header-Based Versioning

Version is passed in an `API-Version` request header. Clean URLs, but requires header inspection.

```python
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import anthropic

client = anthropic.Anthropic()
app = FastAPI()

SUPPORTED_VERSIONS = {"2024-01", "2024-06", "2025-01"}
DEPRECATED_VERSIONS = {"2024-01"}
LATEST_VERSION = "2025-01"


def get_api_version(api_version: str | None = Header(default=None)) -> str:
    version = api_version or LATEST_VERSION
    if version not in SUPPORTED_VERSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported API version '{version}'. "
                f"Supported: {sorted(SUPPORTED_VERSIONS)}. "
                f"Latest: {LATEST_VERSION}"
            ),
        )
    return version


class AgentRequest(BaseModel):
    prompt: str
    model: str = "claude-haiku-4-5-20251001"


@app.post("/agent/run")
async def run_agent(
    req: AgentRequest,
    version: str = __import__("fastapi").Depends(get_api_version),
):
    response = client.messages.create(
        model=req.model,
        max_tokens=1024,
        messages=[{"role": "user", "content": req.prompt}],
    )

    text = response.content[0].text

    # Version-specific response shaping
    if version == "2024-01":
        # Old format: flat string result
        return {"result": text}

    elif version == "2024-06":
        # Added token counts
        return {
            "result": text,
            "usage": {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        }

    else:  # 2025-01
        # Current: structured with deprecation notice for old clients
        headers = {}
        if version in DEPRECATED_VERSIONS:
            headers["Deprecation"] = "true"
            headers["Sunset"] = "2025-07-01"
            headers["Link"] = f'</agent/run>; rel="successor-version"'

        return {
            "result": text,
            "model": response.model,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "version": version,
        }
```

**Expected Token Savings:** Single endpoint handles all versions; no code duplication of routing logic.

**Environment:** Internal APIs where clients can set headers; follows Stripe/GitHub API versioning patterns.

---

## Option 3: Versioned Pydantic Schemas with Adapter Layer

Keep all business logic in a version-agnostic core and adapt request/response at the version boundary.

```python
from fastapi import FastAPI
from pydantic import BaseModel
import anthropic

client = anthropic.Anthropic()
app = FastAPI()


# --- Version-agnostic core ---
class AgentRequest:
    def __init__(self, prompt: str, model: str, max_tokens: int, system: str | None = None):
        self.prompt = prompt
        self.model = model
        self.max_tokens = max_tokens
        self.system = system


class AgentResult:
    def __init__(self, text: str, model: str, input_tokens: int, output_tokens: int):
        self.text = text
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def run_agent_core(req: AgentRequest) -> AgentResult:
    """Pure business logic — no versioning concerns."""
    kwargs = {}
    if req.system:
        kwargs["system"] = req.system

    response = client.messages.create(
        model=req.model,
        max_tokens=req.max_tokens,
        messages=[{"role": "user", "content": req.prompt}],
        **kwargs,
    )
    return AgentResult(
        text=response.content[0].text,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


# --- v1 adapter ---
class V1Request(BaseModel):
    prompt: str


@app.post("/v1/run")
def v1_run(body: V1Request):
    req = AgentRequest(
        prompt=body.prompt,
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
    )
    result = run_agent_core(req)
    return {"result": result.text}  # v1 shape


# --- v2 adapter ---
class V2Request(BaseModel):
    prompt: str
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 1024
    system: str | None = None


class V2Response(BaseModel):
    content: str
    metadata: dict


@app.post("/v2/run", response_model=V2Response)
def v2_run(body: V2Request):
    req = AgentRequest(
        prompt=body.prompt,
        model=body.model,
        max_tokens=body.max_tokens,
        system=body.system,
    )
    result = run_agent_core(req)
    return V2Response(
        content=result.text,
        metadata={
            "model": result.model,
            "tokens": {
                "input": result.input_tokens,
                "output": result.output_tokens,
            },
        },
    )
```

**Expected Token Savings:** Business logic is tested once; adapters are thin and easily audited for correctness.

**Environment:** Any project where versioning should not pollute business logic.

---

## Option 4: Deprecation Headers and Sunset Announcements

Automatically add `Deprecation` and `Sunset` headers to old version responses so clients have machine-readable warnings.

```python
import datetime
from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from typing import Callable
import anthropic

client = anthropic.Anthropic()
app = FastAPI()

VERSION_CONFIG = {
    "v1": {
        "deprecated": True,
        "sunset": datetime.date(2025, 9, 1),
        "successor": "/v2/agent/run",
    },
    "v2": {
        "deprecated": False,
        "sunset": None,
        "successor": None,
    },
}


def add_version_headers(version: str, response: Response):
    config = VERSION_CONFIG.get(version, {})
    response.headers["X-API-Version"] = version

    if config.get("deprecated"):
        response.headers["Deprecation"] = "true"
        if config.get("sunset"):
            response.headers["Sunset"] = config["sunset"].strftime("%a, %d %b %Y 00:00:00 GMT")
        if config.get("successor"):
            response.headers["Link"] = f'<{config["successor"]}>; rel="successor-version"'


@app.post("/v1/agent/run")
async def run_v1(prompt: str, response: Response):
    add_version_headers("v1", response)
    api_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": api_response.content[0].text}


@app.post("/v2/agent/run")
async def run_v2(prompt: str, model: str = "claude-haiku-4-5-20251001", response: Response = None):
    add_version_headers("v2", response)
    api_response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "result": api_response.content[0].text,
        "usage": {
            "input_tokens": api_response.usage.input_tokens,
            "output_tokens": api_response.usage.output_tokens,
        },
    }


@app.get("/versions")
def list_versions():
    return {
        version: {
            "deprecated": cfg["deprecated"],
            "sunset": str(cfg["sunset"]) if cfg["sunset"] else None,
        }
        for version, cfg in VERSION_CONFIG.items()
    }
```

**Expected Token Savings:** Clients receive automated migration warnings; reduces support burden of manually notifying every consumer.

**Environment:** Public APIs with external clients; internal APIs where teams run on different release cadences.

---

## Option 5: SDK Client with Version Negotiation

Bundle a Python SDK that negotiates the API version automatically and hides versioning from SDK consumers.

```python
# synapse_sdk.py — client library for the agent API
import httpx
from pydantic import BaseModel

SUPPORTED_VERSIONS = ["v2", "v1"]


class AgentResponse(BaseModel):
    result: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class SynapseClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        preferred_version: str = "v2",
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._version = preferred_version
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )

    def _negotiate_version(self) -> str:
        """Discover the highest supported version from the server."""
        try:
            resp = self._http.get(f"{self._base_url}/versions")
            resp.raise_for_status()
            server_versions = list(resp.json().keys())
            for v in SUPPORTED_VERSIONS:
                if v in server_versions:
                    return v
        except Exception:
            pass
        return "v1"  # safe fallback

    def run(self, prompt: str, **kwargs) -> AgentResponse:
        url = f"{self._base_url}/{self._version}/agent/run"

        if self._version == "v2":
            payload = {"prompt": prompt, **kwargs}
        else:
            payload = {"prompt": prompt}

        resp = self._http.post(url, json=payload)

        # Auto-downgrade if version not supported
        if resp.status_code == 404 and self._version != "v1":
            self._version = self._negotiate_version()
            return self.run(prompt, **kwargs)

        resp.raise_for_status()
        data = resp.json()

        return AgentResponse(
            result=data.get("result", ""),
            model=data.get("model"),
            input_tokens=data.get("usage", {}).get("input_tokens"),
            output_tokens=data.get("usage", {}).get("output_tokens"),
        )


# Usage
if __name__ == "__main__":
    sdk = SynapseClient(base_url="http://localhost:8000", api_key="your-key")
    result = sdk.run("Explain API versioning in one sentence.")
    print(result.result)
    print(f"Used {result.input_tokens} input tokens")
```

**Expected Token Savings:** SDK hides version complexity from consumers; reduces incorrect API calls from version confusion.

**Environment:** Libraries distributed to internal or external consumers; microservice client SDKs.

---

## Option 6: Version Compatibility Tests

CI tests that verify v1 clients still work after v2 is added, and v2 responses are backward-compatible within the same major version.

```python
import pytest
import httpx
from fastapi.testclient import TestClient

# Import the app from option 1 or 3 above
# from agent_api import app
# client = TestClient(app)

# For illustration, use a mock client
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
import anthropic

mock_response = MagicMock()
mock_response.content = [MagicMock(text="42")]
mock_response.model = "claude-haiku-4-5-20251001"
mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)


@pytest.fixture
def client():
    from fastapi import FastAPI
    app = FastAPI()

    @app.post("/v1/run")
    def v1(prompt: str):
        return {"result": "42"}

    @app.post("/v2/run")
    def v2(prompt: str):
        return {
            "content": "42",
            "metadata": {"model": "claude-haiku-4-5-20251001", "tokens": {"input": 10, "output": 5}},
        }

    return TestClient(app)


class TestV1BackwardCompatibility:
    def test_v1_returns_result_field(self, client):
        resp = client.post("/v1/run", params={"prompt": "test"})
        assert resp.status_code == 200
        assert "result" in resp.json()

    def test_v1_result_is_string(self, client):
        resp = client.post("/v1/run", params={"prompt": "test"})
        assert isinstance(resp.json()["result"], str)

    def test_v1_no_extra_required_fields(self, client):
        # v1 must still work with just `prompt`
        resp = client.post("/v1/run", params={"prompt": "simple"})
        assert resp.status_code == 200


class TestV2Schema:
    def test_v2_has_content_field(self, client):
        resp = client.post("/v2/run", params={"prompt": "test"})
        assert "content" in resp.json()

    def test_v2_has_metadata(self, client):
        resp = client.post("/v2/run", params={"prompt": "test"})
        assert "metadata" in resp.json()
        assert "tokens" in resp.json()["metadata"]

    def test_v2_content_is_string(self, client):
        resp = client.post("/v2/run", params={"prompt": "test"})
        assert isinstance(resp.json()["content"], str)


class TestVersionCoexistence:
    def test_both_versions_available(self, client):
        r1 = client.post("/v1/run", params={"prompt": "x"})
        r2 = client.post("/v2/run", params={"prompt": "x"})
        assert r1.status_code == 200
        assert r2.status_code == 200

    def test_v1_and_v2_same_content(self, client):
        r1 = client.post("/v1/run", params={"prompt": "x"})
        r2 = client.post("/v2/run", params={"prompt": "x"})
        assert r1.json()["result"] == r2.json()["content"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

**Expected Token Savings:** Catches breaking changes before deployment; eliminates emergency rollbacks from schema regressions.

**Environment:** CI pipeline; any project with multiple clients on different version cadences.

---

## Comparison

| Option | Version Location | URL Cleanliness | Client Complexity | Deprecation Support |
|--------|-----------------|-----------------|------------------|---------------------|
| 1. URL path versioning | URL prefix | Low | None | Manual |
| 2. Header versioning | Header | High | Header required | `Deprecation` header |
| 3. Adapter layer | URL prefix | Low | None | Manual |
| 4. Deprecation headers | URL prefix | Low | None | Automated headers |
| 5. SDK with negotiation | Abstracted | N/A | SDK only | Auto-downgrade |
| 6. Compatibility tests | N/A | N/A | None | Regression-checked |
