---
layout: solution
title: "Agent Doesn't Implement Tool Result Normalization Across Providers"
category: tool-failure
description: "Different tool providers return data in wildly different formats — normalize all tool results to a canonical schema before the LLM processes them, preventing format-induced failures."
tags: [tool-failure, normalization, schema, providers, canonical, data-transformation]
---

## Problem

An agent calls a weather API that returns `{"temp_f": 72, "cond": "sunny"}`. The next day the provider upgrades and returns `{"temperature": {"value": 72, "unit": "fahrenheit"}, "conditions": "sunny"}`. The agent breaks. Or the agent uses three different search providers — each returns results in a completely different shape. Without a normalization layer, every provider change becomes a breaking change, and the LLM receives inconsistent data it must silently adapt to.

```python
# Naive: raw tool result sent directly to LLM — format varies by provider
def call_tool(tool_name: str, args: dict) -> str:
    raw = providers[tool_name].call(args)
    return json.dumps(raw)  # format inconsistency propagates to LLM
```

## Solution Options

### Option 1: Typed Canonical Schema with Provider Adapters

Define a canonical result type for each tool category. Implement a provider-specific adapter that maps raw provider output to the canonical form.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Any

# Canonical schema for search results
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    rank: int
    source: str

@dataclass
class CanonicalSearchResponse:
    query: str
    results: list[SearchResult]
    total_count: int
    provider: str

def _normalize_google_search(raw: dict, query: str) -> CanonicalSearchResponse:
    items = raw.get("items", [])
    return CanonicalSearchResponse(
        query=query,
        results=[
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                rank=i + 1,
                source="google",
            )
            for i, item in enumerate(items)
        ],
        total_count=int(raw.get("searchInformation", {}).get("totalResults", 0)),
        provider="google",
    )

def _normalize_bing_search(raw: dict, query: str) -> CanonicalSearchResponse:
    pages = raw.get("webPages", {}).get("value", [])
    return CanonicalSearchResponse(
        query=query,
        results=[
            SearchResult(
                title=page.get("name", ""),
                url=page.get("url", ""),
                snippet=page.get("snippet", ""),
                rank=i + 1,
                source="bing",
            )
            for i, page in enumerate(pages)
        ],
        total_count=raw.get("webPages", {}).get("totalEstimatedMatches", 0),
        provider="bing",
    )

def _normalize_duckduckgo_search(raw: dict, query: str) -> CanonicalSearchResponse:
    results_raw = raw.get("Results", []) + raw.get("RelatedTopics", [])
    results = []
    for i, item in enumerate(results_raw):
        if "FirstURL" in item:
            results.append(SearchResult(
                title=item.get("Text", "")[:60],
                url=item.get("FirstURL", ""),
                snippet=item.get("Text", ""),
                rank=i + 1,
                source="duckduckgo",
            ))
    return CanonicalSearchResponse(
        query=query,
        results=results[:10],
        total_count=len(results),
        provider="duckduckgo",
    )

SEARCH_NORMALIZERS = {
    "google": _normalize_google_search,
    "bing": _normalize_bing_search,
    "duckduckgo": _normalize_duckduckgo_search,
}

def normalize_search_result(raw: dict, query: str, provider: str) -> str:
    normalizer = SEARCH_NORMALIZERS.get(provider)
    if normalizer is None:
        return json.dumps({"error": f"Unknown provider: {provider}", "raw": raw})
    canonical = normalizer(raw, query)
    return json.dumps({
        "query": canonical.query,
        "provider": canonical.provider,
        "total": canonical.total_count,
        "results": [
            {"rank": r.rank, "title": r.title, "url": r.url, "snippet": r.snippet}
            for r in canonical.results[:5]
        ],
    })


client = anthropic.Anthropic()

# Simulate different provider responses
google_raw = {
    "items": [
        {"title": "Python Tutorial", "link": "https://python.org/tutorial", "snippet": "Learn Python basics."},
        {"title": "Real Python", "link": "https://realpython.com", "snippet": "Advanced Python tutorials."},
    ],
    "searchInformation": {"totalResults": "1250000"},
}
bing_raw = {
    "webPages": {
        "value": [
            {"name": "Python Docs", "url": "https://docs.python.org", "snippet": "Official Python documentation."},
        ],
        "totalEstimatedMatches": 890000,
    }
}

# Both providers normalized to same schema
google_normalized = normalize_search_result(google_raw, "learn Python", "google")
bing_normalized = normalize_search_result(bing_raw, "learn Python", "bing")

# LLM sees consistent format regardless of provider
r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{
        "role": "user",
        "content": f"Summarize these search results:\nProvider A: {google_normalized}\nProvider B: {bing_normalized}",
    }],
)
print(r.content[0].text[:300])

# Expected Token Savings: Canonical format is more compact than raw provider JSON; consistent schema reduces LLM confusion tokens
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Schema Registry with JSON Schema Validation and Coercion

Maintain a schema registry for each tool. Validate raw results against the expected schema; coerce or fill defaults where fields are missing or misnamed.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Any

@dataclass
class FieldMapping:
    canonical_name: str
    source_paths: list[str]   # ordered list of paths to try, e.g. ["temp_f", "temperature.value"]
    default: Any = None
    transform: callable = None

class SchemaCoercer:
    def __init__(self, mappings: list[FieldMapping]):
        self.mappings = mappings

    def _get_nested(self, data: dict, path: str) -> Any:
        """Get value at dot-notated path: 'temperature.value' → data['temperature']['value']"""
        parts = path.split(".")
        current = data
        for part in parts:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def coerce(self, raw: dict) -> dict:
        result = {}
        for mapping in self.mappings:
            value = None
            for path in mapping.source_paths:
                value = self._get_nested(raw, path)
                if value is not None:
                    break
            if value is None:
                value = mapping.default
            if mapping.transform and value is not None:
                try:
                    value = mapping.transform(value)
                except Exception:
                    value = mapping.default
            result[mapping.canonical_name] = value
        return result


# Define canonical schema for weather data
WEATHER_SCHEMA = SchemaCoercer([
    FieldMapping("temperature_celsius", ["temp_c", "temperature.celsius", "main.temp"],
                 transform=lambda v: float(v)),
    FieldMapping("temperature_fahrenheit", ["temp_f", "temperature.fahrenheit"],
                 transform=lambda v: float(v)),
    FieldMapping("condition", ["condition", "cond", "weather.0.description", "conditions"]),
    FieldMapping("humidity_percent", ["humidity", "humidity_pct", "main.humidity"],
                 default=None, transform=lambda v: int(v)),
    FieldMapping("wind_speed_mph", ["wind_mph", "wind.speed", "wind_speed"],
                 default=None, transform=lambda v: float(v)),
    FieldMapping("location", ["location", "city", "name", "location.city"]),
])

def normalize_weather(raw: dict) -> str:
    canonical = WEATHER_SCHEMA.coerce(raw)
    # Derive missing temperatures
    if canonical["temperature_celsius"] is None and canonical["temperature_fahrenheit"] is not None:
        canonical["temperature_celsius"] = round((canonical["temperature_fahrenheit"] - 32) * 5 / 9, 1)
    elif canonical["temperature_fahrenheit"] is None and canonical["temperature_celsius"] is not None:
        canonical["temperature_fahrenheit"] = round(canonical["temperature_celsius"] * 9 / 5 + 32, 1)
    return json.dumps(canonical)


client = anthropic.Anthropic()

# Three different weather API response formats
provider_a = {"temp_f": 72, "cond": "sunny", "city": "New York"}
provider_b = {"temperature": {"celsius": 22.2, "fahrenheit": 72}, "conditions": "Clear sky",
              "location": {"city": "New York"}, "humidity": 45}
provider_c = {"main": {"temp": 22.2, "humidity": 45}, "weather": [{"description": "clear sky"}],
              "name": "New York", "wind": {"speed": 5.2}}

for label, raw in [("Provider A", provider_a), ("Provider B", provider_b), ("Provider C", provider_c)]:
    normalized = normalize_weather(raw)
    print(f"{label}: {normalized}")

# LLM receives uniform weather data regardless of source
r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content":
        f"Compare weather data from three sources:\n{normalize_weather(provider_a)}\n"
        f"{normalize_weather(provider_b)}\n{normalize_weather(provider_c)}"}],
)
print(f"\nLLM comparison: {r.content[0].text[:200]}")

# Expected Token Savings: Normalized JSON removes redundant nesting/metadata; ~20-40% smaller than raw provider JSON
# Environment: ANTHROPIC_API_KEY
```

### Option 3: LLM-Powered Normalization for Unknown Schemas

When the provider schema is unknown or changes unexpectedly, use a lightweight LLM call to extract canonical fields from arbitrary raw output.

```python
import anthropic
import json
from dataclasses import dataclass
from functools import lru_cache

@dataclass
class NormalizationTarget:
    canonical_fields: dict[str, str]  # field_name → description

client = anthropic.Anthropic()

NORMALIZE_PROMPT = """Extract these fields from the raw data. Return JSON with exactly these keys.
If a field cannot be found, use null.

Target fields:
{field_descriptions}

Raw data:
{raw_data}

Return only valid JSON with the target fields."""

@lru_cache(maxsize=128)
def _cached_normalize(raw_json: str, fields_json: str) -> str:
    fields = json.loads(fields_json)
    field_desc = "\n".join(f"- {name}: {desc}" for name, desc in fields.items())
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": NORMALIZE_PROMPT.format(
            field_descriptions=field_desc,
            raw_data=raw_json[:1000],
        )}],
    )
    return r.content[0].text

def llm_normalize(raw: dict, target: NormalizationTarget) -> dict:
    raw_json = json.dumps(raw)
    fields_json = json.dumps(target.canonical_fields)
    # Use cached normalization for identical schemas
    cache_key = f"{hash(raw_json[:200])}:{fields_json}"
    try:
        result_str = _cached_normalize(raw_json, fields_json)
        return json.loads(result_str)
    except Exception:
        return {k: None for k in target.canonical_fields}


# Canonical product schema — works regardless of e-commerce API format
product_target = NormalizationTarget(canonical_fields={
    "product_id": "unique product identifier",
    "name": "product display name",
    "price_usd": "price in US dollars as a float",
    "in_stock": "boolean availability",
    "category": "product category",
    "rating": "average rating out of 5.0",
})

# Different e-commerce APIs with completely different schemas
shopify_raw = {
    "id": 789, "title": "Wireless Mouse", "variants": [{"price": "29.99"}],
    "available": True, "product_type": "Electronics", "metafields": {"avg_rating": 4.2}
}
woocommerce_raw = {
    "product_id": 456, "post_title": "Wireless Mouse", "regular_price": "29.99",
    "stock_status": "instock", "categories": [{"name": "Electronics"}], "average_rating": "4.2"
}
amazon_raw = {
    "ASIN": "B001234", "ItemAttributes": {"Title": "Wireless Mouse", "ListPrice": {"Amount": "2999"}},
    "OfferSummary": {"TotalNew": "5"}, "SalesRank": 1200,
}

for label, raw in [("Shopify", shopify_raw), ("WooCommerce", woocommerce_raw), ("Amazon", amazon_raw)]:
    normalized = llm_normalize(raw, product_target)
    print(f"{label}: {json.dumps(normalized)}")

# Expected Token Savings: ~150 tokens per normalization call; cache hit rate reduces ongoing cost significantly
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Middleware Normalization Pipeline with Error Recovery

Build a pipeline that applies transformations sequentially. Each stage can fail gracefully — missing fields get defaults, malformed values get replaced with safe fallbacks.

```python
import anthropic
import json
from dataclasses import dataclass, field
from typing import Any, Callable

@dataclass
class PipelineStage:
    name: str
    transform: Callable[[dict], dict]
    on_error: str = "skip"  # "skip" | "raise" | "default"
    default_value: Any = None

class NormalizationPipeline:
    def __init__(self, stages: list[PipelineStage]):
        self.stages = stages
        self.errors: list[str] = []

    def run(self, raw: dict) -> dict:
        self.errors.clear()
        result = dict(raw)
        for stage in self.stages:
            try:
                result = stage.transform(result)
            except Exception as e:
                self.errors.append(f"{stage.name}: {e}")
                if stage.on_error == "raise":
                    raise
                elif stage.on_error == "default" and stage.default_value is not None:
                    result[stage.name] = stage.default_value
                # "skip" — continue with unchanged result
        return result


def _flatten_nested(data: dict, prefix: str = "", separator: str = "_") -> dict:
    """Recursively flatten nested dicts."""
    flat = {}
    for key, value in data.items():
        new_key = f"{prefix}{separator}{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_nested(value, new_key, separator))
        else:
            flat[new_key] = value
    return flat

def _normalize_field_names(data: dict) -> dict:
    """Convert camelCase and PascalCase to snake_case."""
    import re
    result = {}
    for key, value in data.items():
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
        result[snake] = value
    return result

def _coerce_types(data: dict) -> dict:
    """Coerce common type mismatches."""
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            if value.lower() in ("true", "yes", "1"):
                result[key] = True
            elif value.lower() in ("false", "no", "0"):
                result[key] = False
            elif value.replace(".", "").replace("-", "").isdigit():
                result[key] = float(value) if "." in value else int(value)
            else:
                result[key] = value
        else:
            result[key] = value
    return result

def _select_canonical_fields(canonical_keys: list[str]) -> Callable[[dict], dict]:
    def _select(data: dict) -> dict:
        return {k: data.get(k) for k in canonical_keys}
    return _select


CANONICAL_KEYS = ["product_id", "name", "price", "in_stock", "category", "rating"]

pipeline = NormalizationPipeline([
    PipelineStage("flatten", _flatten_nested, on_error="skip"),
    PipelineStage("snake_case", _normalize_field_names, on_error="skip"),
    PipelineStage("coerce_types", _coerce_types, on_error="skip"),
    PipelineStage("select_fields", _select_canonical_fields(CANONICAL_KEYS), on_error="skip"),
])

client = anthropic.Anthropic()

messy_provider_output = {
    "ProductID": "abc-123",
    "productName": "Wireless Keyboard",
    "Price": {"amount": "49.99", "currency": "USD"},
    "IsInStock": "true",
    "Category": {"primaryName": "Electronics"},
    "AverageRating": 4.5,
}

normalized = pipeline.run(messy_provider_output)
print(f"Normalized: {json.dumps(normalized, indent=2)}")
if pipeline.errors:
    print(f"Non-fatal errors: {pipeline.errors}")

# Use in LLM call
r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content":
        f"Describe this product in one sentence: {json.dumps(normalized)}"}],
)
print(f"\nLLM: {r.content[0].text}")

# Expected Token Savings: Pipeline produces compact, flat JSON; no nested noise for LLM to parse
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Async Parallel Normalization with Schema Version Detection

When tools return results, detect the schema version from the response shape and apply the matching normalizer — without breaking on unknown versions.

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass
from typing import Callable

@dataclass
class VersionedNormalizer:
    version_id: str
    detector: Callable[[dict], bool]   # returns True if this normalizer matches the raw data
    normalizer: Callable[[dict], dict]

class VersionDetectingNormalizer:
    def __init__(self, normalizers: list[VersionedNormalizer], fallback: Callable[[dict], dict] = None):
        self.normalizers = normalizers
        self.fallback = fallback or (lambda x: x)
        self.version_stats: dict[str, int] = {}

    def normalize(self, raw: dict) -> dict:
        for vn in self.normalizers:
            if vn.detector(raw):
                self.version_stats[vn.version_id] = self.version_stats.get(vn.version_id, 0) + 1
                return vn.normalizer(raw)
        print(f"[SCHEMA] Unknown schema — using fallback. Keys: {list(raw.keys())[:5]}")
        return self.fallback(raw)


# Stock price API normalizer with multiple schema versions
stock_normalizer = VersionDetectingNormalizer([
    VersionedNormalizer(
        version_id="alpha_vantage_v1",
        detector=lambda r: "Global Quote" in r,
        normalizer=lambda r: {
            "symbol": r["Global Quote"].get("01. symbol"),
            "price": float(r["Global Quote"].get("05. price", 0)),
            "change_pct": r["Global Quote"].get("10. change percent", "0%").rstrip("%"),
            "volume": int(r["Global Quote"].get("06. volume", 0)),
            "provider": "alpha_vantage_v1",
        },
    ),
    VersionedNormalizer(
        version_id="polygon_v2",
        detector=lambda r: "results" in r and isinstance(r.get("results"), dict),
        normalizer=lambda r: {
            "symbol": r.get("ticker"),
            "price": r["results"].get("c"),
            "change_pct": str(round(((r["results"].get("c", 0) - r["results"].get("o", 1)) /
                                     max(r["results"].get("o", 1), 0.01)) * 100, 2)),
            "volume": r["results"].get("v"),
            "provider": "polygon_v2",
        },
    ),
    VersionedNormalizer(
        version_id="finnhub_v1",
        detector=lambda r: "c" in r and "o" in r and "h" in r and "l" in r,
        normalizer=lambda r: {
            "symbol": r.get("s", "UNKNOWN"),
            "price": r.get("c"),
            "change_pct": str(round(((r.get("c", 0) - r.get("o", 1)) / max(r.get("o", 1), 0.01)) * 100, 2)),
            "volume": r.get("v"),
            "provider": "finnhub_v1",
        },
    ),
])

client = anthropic.AsyncAnthropic()

async def fetch_and_normalize(symbol: str, provider_raw: dict) -> dict:
    normalized = stock_normalizer.normalize(provider_raw)
    normalized["queried_symbol"] = symbol
    return normalized

async def multi_provider_stock_query(symbol: str) -> str:
    # Simulate fetching from multiple providers simultaneously
    raw_responses = {
        "alpha_vantage": {"Global Quote": {"01. symbol": symbol, "05. price": "185.50",
                                           "10. change percent": "1.25%", "06. volume": "45000000"}},
        "polygon": {"ticker": symbol, "results": {"c": 185.50, "o": 183.20, "h": 186.0, "l": 182.5, "v": 45000000}},
        "finnhub": {"c": 185.50, "o": 183.20, "h": 186.0, "l": 182.5, "v": 45000000, "s": symbol},
    }
    normalized_list = await asyncio.gather(*[
        fetch_and_normalize(symbol, raw) for raw in raw_responses.values()
    ])
    # Consensus price
    prices = [n["price"] for n in normalized_list if n.get("price")]
    avg_price = sum(float(p) for p in prices) / len(prices) if prices else None
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content":
            f"Stock data for {symbol} from {len(normalized_list)} providers: {json.dumps(normalized_list)}\n"
            f"Average price: ${avg_price:.2f}. Summarize in one sentence."}],
    )
    return r.content[0].text

async def main():
    result = await multi_provider_stock_query("AAPL")
    print(result)
    print(f"Schema version stats: {stock_normalizer.version_stats}")

asyncio.run(main())

# Expected Token Savings: Normalized output removes provider-specific noise; parallel normalization adds no latency
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Tool Result Schema Migration for Breaking Provider Changes

When a provider changes its schema, maintain a migration registry that automatically upgrades old-format results to the current canonical form.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Callable

@dataclass
class SchemaMigration:
    from_version: str
    to_version: str
    migrate: Callable[[dict], dict]
    description: str

class MigrationRegistry:
    def __init__(self):
        self.migrations: list[SchemaMigration] = []
        self.version_detector: Callable[[dict], str] | None = None

    def register(self, migration: SchemaMigration) -> None:
        self.migrations.append(migration)

    def set_detector(self, detector: Callable[[dict], str]) -> None:
        self.version_detector = detector

    def _find_path(self, from_ver: str, to_ver: str) -> list[SchemaMigration]:
        """Find migration path via BFS."""
        from collections import deque
        queue = deque([[from_ver]])
        visited = {from_ver}
        migration_map: dict[str, list[SchemaMigration]] = {}
        for m in self.migrations:
            migration_map.setdefault(m.from_version, []).append(m)

        while queue:
            path_versions = queue.popleft()
            current = path_versions[-1]
            if current == to_ver:
                # Reconstruct migration path
                migrations = []
                for i in range(len(path_versions) - 1):
                    for m in migration_map.get(path_versions[i], []):
                        if m.to_version == path_versions[i + 1]:
                            migrations.append(m)
                return migrations
            for m in migration_map.get(current, []):
                if m.to_version not in visited:
                    visited.add(m.to_version)
                    queue.append(path_versions + [m.to_version])
        return []

    def normalize(self, raw: dict, target_version: str = "v3") -> dict:
        if self.version_detector is None:
            return raw
        current_version = self.version_detector(raw)
        if current_version == target_version:
            return raw
        path = self._find_path(current_version, target_version)
        if not path:
            print(f"[MIGRATION] No path from {current_version} to {target_version}")
            return raw
        result = dict(raw)
        for step in path:
            print(f"[MIGRATION] {step.from_version} → {step.to_version}: {step.description}")
            result = step.migrate(result)
        return result


# Weather API schema evolution
registry = MigrationRegistry()

def detect_weather_version(raw: dict) -> str:
    if "temp_f" in raw:
        return "v1"
    if "temperature" in raw and isinstance(raw["temperature"], (int, float)):
        return "v2"
    if "temperature" in raw and isinstance(raw["temperature"], dict):
        return "v3"
    return "unknown"

registry.set_detector(detect_weather_version)

registry.register(SchemaMigration(
    from_version="v1", to_version="v2",
    migrate=lambda r: {
        "temperature": (r["temp_f"] - 32) * 5 / 9,
        "unit": "celsius",
        "condition": r.get("cond", r.get("condition")),
        "city": r.get("city", r.get("location")),
    },
    description="Flatten + convert F→C",
))
registry.register(SchemaMigration(
    from_version="v2", to_version="v3",
    migrate=lambda r: {
        "temperature": {"value": r["temperature"], "unit": r.get("unit", "celsius")},
        "condition": {"description": r.get("condition"), "icon": None},
        "location": {"city": r.get("city"), "country": None},
    },
    description="Nest temperature and location objects",
))


client = anthropic.Anthropic()

# All three provider versions normalized to v3
v1_response = {"temp_f": 72, "cond": "sunny", "city": "NYC"}
v2_response = {"temperature": 22.2, "unit": "celsius", "condition": "Sunny", "city": "NYC"}
v3_response = {"temperature": {"value": 22.2, "unit": "celsius"},
               "condition": {"description": "Sunny", "icon": "sun"},
               "location": {"city": "NYC", "country": "US"}}

for label, raw in [("v1", v1_response), ("v2", v2_response), ("v3", v3_response)]:
    normalized = registry.normalize(raw, target_version="v3")
    print(f"{label} → v3: {json.dumps(normalized)}")

# LLM always sees v3 format
normalized = registry.normalize(v1_response, target_version="v3")
r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": f"Describe this weather in one sentence: {json.dumps(normalized)}"}],
)
print(f"\nLLM: {r.content[0].text}")

# Expected Token Savings: Migration ensures compact canonical format; no need to prompt-engineer around format variance
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Approach | Handles Unknown Schemas | Breaking Change Resilience | Best For |
|--------|---------|------------------------|--------------------------|----------|
| 1. Typed Adapters | Per-provider adapter functions | No | Manual adapter update | Known, stable providers |
| 2. Schema Coercer | Field mapping with source paths | Partial | Add new paths | Multiple field name variants |
| 3. LLM Normalization | LLM extracts canonical fields | Yes | Automatic | Unknown/changing schemas |
| 4. Pipeline Stages | Sequential transform stages | Partial | Stage-level recovery | Complex multi-step normalization |
| 5. Version Detector | Pattern-based version routing | Partial (fallback) | Add new detector | Same provider, multiple versions |
| 6. Migration Registry | Graph-based schema migration | No | Automated migration path | Provider schema evolution over time |

**Recommended**: Option 1 (typed adapters) + Option 6 (migrations) for production systems. Option 3 (LLM normalization) as a fallback for unknown schemas during rapid iteration.
