---
title: "Agent Doesn't Implement Simulation-Based Integration Testing"
description: "Test agent behavior end-to-end by simulating realistic user scenarios, tool responses, and environmental conditions — without hitting live APIs or production systems."
category: testing
difficulty: advanced
tags: [testing, simulation, integration-testing, mocking, end-to-end, reliability]
---

# Agent Doesn't Implement Simulation-Based Integration Testing

## Problem

Unit tests verify individual components in isolation, but agent failures often occur at integration points: when the model's response triggers the wrong tool, when a tool returns unexpected data, or when a sequence of turns produces unexpected emergent behavior. Simulation-based integration testing runs the full agent loop against realistic simulated environments — catching these multi-step failures before they hit production.

---

## Option 1: Simulated Tool Environment with Scripted Responses

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from typing import Callable, Any

client = anthropic.AsyncAnthropic()

@dataclass
class SimulatedTool:
    name: str
    response_script: list[Any]  # sequential responses, or a callable
    call_count: int = 0
    calls_received: list[dict] = field(default_factory=list)

    def respond(self, input_data: dict) -> Any:
        self.calls_received.append(input_data)
        if callable(self.response_script):
            return self.response_script(input_data, self.call_count)
        idx = min(self.call_count, len(self.response_script) - 1)
        result = self.response_script[idx]
        self.call_count += 1
        return result

class SimulatedEnvironment:
    def __init__(self, tools: list[SimulatedTool]):
        self.tools = {t.name: t for t in tools}
        self.turn_count = 0

    def get_tool_definitions(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": f"Simulated tool: {t.name}",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": True}
            }
            for t in self.tools.values()
        ]

    async def run_agent_turn(self, messages: list[dict], system: str = "") -> tuple[list[dict], bool]:
        """Run one agent turn, handling tool calls against simulated tools."""
        self.turn_count += 1
        kwargs = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "tools": self.get_tool_definitions(),
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        resp = await client.messages.create(**kwargs)
        new_messages = list(messages)

        if resp.stop_reason == "end_turn":
            # Collect text response
            text = "".join(b.text for b in resp.content if hasattr(b, "text"))
            new_messages.append({"role": "assistant", "content": resp.content})
            return new_messages, True  # done

        if resp.stop_reason == "tool_use":
            new_messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    tool = self.tools.get(block.name)
                    if tool:
                        result = tool.respond(block.input)
                    else:
                        result = {"error": f"Unknown tool: {block.name}"}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result)
                    })
            new_messages.append({"role": "user", "content": tool_results})
            return new_messages, False  # continue

        return new_messages, True

    async def run_scenario(self, user_message: str, system: str = "", max_turns: int = 10) -> dict:
        messages = [{"role": "user", "content": user_message}]
        done = False
        turns = 0
        while not done and turns < max_turns:
            messages, done = await self.run_agent_turn(messages, system)
            turns += 1

        final_text = ""
        for msg in reversed(messages):
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if hasattr(block, "text"):
                        final_text = block.text
                        break
            if final_text:
                break

        return {
            "turns": turns,
            "final_response": final_text,
            "tool_calls": {name: t.call_count for name, t in self.tools.items()},
            "messages": len(messages),
        }

async def test_search_and_summarize():
    """Test that the agent correctly uses search tool and summarizes results."""
    env = SimulatedEnvironment(tools=[
        SimulatedTool(
            name="web_search",
            response_script=[
                {"results": [{"title": "Python asyncio Guide", "snippet": "asyncio provides async I/O in Python 3.4+"}]},
                {"results": [{"title": "Async Best Practices", "snippet": "Use asyncio.gather for concurrent execution"}]},
            ]
        )
    ])

    result = await env.run_scenario(
        "Search for information about Python asyncio and summarize what you find.",
        system="You are a research assistant. Use the web_search tool to find information."
    )

    print(f"Test: search_and_summarize")
    print(f"  Turns: {result['turns']}, Tool calls: {result['tool_calls']}")
    print(f"  Final: {result['final_response'][:100]}")

    # Assertions
    assert result['tool_calls'].get('web_search', 0) >= 1, "Should have called web_search at least once"
    assert len(result['final_response']) > 50, "Should have produced a meaningful summary"
    print("  ✓ PASSED\n")

asyncio.run(test_search_and_summarize())
```

---

## Option 2: Scenario-Based Test Suite with Assertions

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.AsyncAnthropic()

@dataclass
class ScenarioResult:
    scenario_name: str
    passed: bool
    turns_taken: int
    tool_calls: dict[str, int]
    final_response: str
    assertion_failures: list[str] = field(default_factory=list)

@dataclass
class Scenario:
    name: str
    user_message: str
    system_prompt: str
    simulated_tools: dict[str, Callable]  # tool_name -> handler
    assertions: list[Callable[[ScenarioResult], bool]]
    assertion_names: list[str]
    max_turns: int = 8

async def run_scenario(scenario: Scenario) -> ScenarioResult:
    tool_call_counts: dict[str, int] = {name: 0 for name in scenario.simulated_tools}
    messages = [{"role": "user", "content": scenario.user_message}]
    turns = 0
    final_response = ""

    tool_defs = [
        {"name": name, "description": f"Tool: {name}",
         "input_schema": {"type": "object", "properties": {}, "additionalProperties": True}}
        for name in scenario.simulated_tools
    ]

    while turns < scenario.max_turns:
        turns += 1
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=scenario.system_prompt,
            tools=tool_defs,
            messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if hasattr(block, "text"):
                    final_response = block.text
            break

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    tool_call_counts[block.name] = tool_call_counts.get(block.name, 0) + 1
                    handler = scenario.simulated_tools.get(block.name)
                    result = handler(block.input) if handler else {"error": "Unknown tool"}
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(result)})
            messages.append({"role": "user", "content": tool_results})

    sr = ScenarioResult(
        scenario_name=scenario.name,
        passed=True,
        turns_taken=turns,
        tool_calls=tool_call_counts,
        final_response=final_response
    )

    # Run assertions
    for assertion, name in zip(scenario.assertions, scenario.assertion_names):
        try:
            if not assertion(sr):
                sr.assertion_failures.append(f"FAILED: {name}")
                sr.passed = False
        except Exception as e:
            sr.assertion_failures.append(f"ERROR in {name}: {e}")
            sr.passed = False

    return sr

async def run_test_suite():
    scenarios = [
        Scenario(
            name="calculator_use",
            user_message="What is 15% of 847?",
            system_prompt="You are a helpful assistant. Use the calculator tool for arithmetic.",
            simulated_tools={
                "calculator": lambda inp: {"result": eval(inp.get("expression", "0"))}
            },
            assertions=[
                lambda r: r.tool_calls.get("calculator", 0) >= 1,
                lambda r: "127" in r.final_response or "127.05" in r.final_response,
                lambda r: r.turns_taken <= 3,
            ],
            assertion_names=["used_calculator", "correct_answer_in_response", "completed_quickly"]
        ),
        Scenario(
            name="data_retrieval_and_format",
            user_message="Get the user profile for ID 42 and format it as a table.",
            system_prompt="You are a data assistant.",
            simulated_tools={
                "get_user": lambda inp: {"id": inp.get("user_id", 0), "name": "Alice", "email": "alice@example.com", "role": "admin"}
            },
            assertions=[
                lambda r: r.tool_calls.get("get_user", 0) == 1,
                lambda r: "Alice" in r.final_response,
                lambda r: "|" in r.final_response or "table" in r.final_response.lower(),
            ],
            assertion_names=["called_get_user_once", "name_in_response", "table_formatted"]
        ),
    ]

    results = await asyncio.gather(*[run_scenario(s) for s in scenarios])
    print("=" * 60)
    print("SIMULATION TEST SUITE RESULTS")
    print("=" * 60)
    for r in results:
        status = "✓ PASS" if r.passed else "✗ FAIL"
        print(f"\n{status}: {r.scenario_name}")
        print(f"  Turns: {r.turns_taken}, Tool calls: {r.tool_calls}")
        print(f"  Response: {r.final_response[:80]}...")
        if r.assertion_failures:
            for f in r.assertion_failures:
                print(f"  {f}")

    passed = sum(1 for r in results if r.passed)
    print(f"\nTotal: {passed}/{len(results)} scenarios passed")

asyncio.run(run_test_suite())
```

---

## Option 3: Adversarial Simulation (Edge Cases and Failures)

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class FailureScenario:
    name: str
    tool_responses: dict  # tool_name -> response (can be exception)
    user_input: str
    expected_behavior: str  # what the agent should do

async def run_failure_scenario(scenario: FailureScenario, system: str) -> dict:
    messages = [{"role": "user", "content": scenario.user_input}]
    tool_defs = [
        {"name": name, "description": f"Tool: {name}",
         "input_schema": {"type": "object", "properties": {}, "additionalProperties": True}}
        for name in scenario.tool_responses
    ]
    turns = 0
    final_response = ""
    errors_encountered = []

    while turns < 6:
        turns += 1
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            tools=tool_defs,
            messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            for b in resp.content:
                if hasattr(b, "text"):
                    final_response = b.text
            break

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    response = scenario.tool_responses.get(block.name, {"error": "not found"})
                    if isinstance(response, Exception):
                        result_content = f"Error: {response}"
                        errors_encountered.append(str(response))
                    elif isinstance(response, dict) and "error" in response:
                        result_content = f"Error: {response['error']}"
                        errors_encountered.append(response["error"])
                    else:
                        result_content = str(response)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_content,
                        "is_error": "Error:" in result_content
                    })
            messages.append({"role": "user", "content": tool_results})

    return {
        "scenario": scenario.name,
        "final_response": final_response,
        "errors_encountered": errors_encountered,
        "turns": turns,
        "gracefully_handled": len(final_response) > 20 and not final_response.startswith("Error"),
    }

async def adversarial_test_suite():
    system = "You are a helpful assistant. Handle tool errors gracefully and always provide a helpful response to the user."

    failure_scenarios = [
        FailureScenario(
            name="tool_timeout",
            tool_responses={"database_query": {"error": "Connection timeout after 30s"}},
            user_input="Look up the order status for order #12345.",
            expected_behavior="Acknowledge error, suggest retry or alternative"
        ),
        FailureScenario(
            name="tool_returns_empty",
            tool_responses={"search": {"results": []}},
            user_input="Find information about Zxyqrplt (a nonsense word).",
            expected_behavior="Report no results found, offer to rephrase"
        ),
        FailureScenario(
            name="tool_returns_unexpected_format",
            tool_responses={"get_price": "not_a_dict_but_a_string"},
            user_input="What is the current price of product X?",
            expected_behavior="Handle unexpected format gracefully"
        ),
    ]

    results = await asyncio.gather(*[run_failure_scenario(s, system) for s in failure_scenarios])
    print("ADVERSARIAL SIMULATION RESULTS")
    print("=" * 50)
    for r in results:
        graceful = "✓ Graceful" if r["gracefully_handled"] else "✗ Crashed"
        print(f"\n{graceful}: {r['scenario']}")
        print(f"  Errors: {r['errors_encountered']}")
        print(f"  Response: {r['final_response'][:100]}")

asyncio.run(adversarial_test_suite())
```

---

## Option 4: Multi-Turn Conversation Simulation

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class ConversationScript:
    """Simulated multi-turn conversation for integration testing."""
    name: str
    turns: list[str]  # user messages in sequence
    tool_responses: dict[str, list]  # tool_name -> sequential responses
    assertions: list[dict]  # {"after_turn": N, "check": callable, "name": str}
    system: str = "You are a helpful assistant."

async def run_conversation_simulation(script: ConversationScript) -> dict:
    messages: list[dict] = []
    tool_call_sequence: list[dict] = []
    tool_response_idx: dict[str, int] = {name: 0 for name in script.tool_responses}
    assertion_results: list[dict] = []

    tool_defs = [
        {"name": name, "description": f"Tool: {name}",
         "input_schema": {"type": "object", "properties": {}, "additionalProperties": True}}
        for name in script.tool_responses
    ]

    for turn_idx, user_message in enumerate(script.turns):
        messages.append({"role": "user", "content": user_message})
        turn_done = False

        while not turn_done:
            resp = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=script.system,
                tools=tool_defs,
                messages=messages
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                turn_done = True

            elif resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        responses = script.tool_responses.get(block.name, [{"result": "ok"}])
                        idx = tool_response_idx.get(block.name, 0)
                        response = responses[min(idx, len(responses)-1)]
                        tool_response_idx[block.name] = idx + 1
                        tool_call_sequence.append({"tool": block.name, "input": block.input, "turn": turn_idx})
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(response)})
                messages.append({"role": "user", "content": tool_results})

        # Run turn-specific assertions
        last_response = ""
        for msg in reversed(messages):
            if isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if hasattr(b, "text"):
                        last_response = b.text
                        break
            if last_response:
                break

        for assertion in script.assertions:
            if assertion["after_turn"] == turn_idx:
                passed = assertion["check"](last_response, tool_call_sequence)
                assertion_results.append({"name": assertion["name"], "passed": passed, "turn": turn_idx})
                print(f"  [T{turn_idx}] {'✓' if passed else '✗'} {assertion['name']}")

    return {"script": script.name, "assertion_results": assertion_results, "tool_calls": tool_call_sequence}

async def main():
    script = ConversationScript(
        name="shopping_assistant_flow",
        system="You are a shopping assistant. Help users find and purchase products.",
        turns=[
            "I'm looking for a laptop under $1000.",
            "Tell me more about the first option.",
            "I'll take it. Please add it to my cart.",
        ],
        tool_responses={
            "search_products": [
                {"products": [{"id": 1, "name": "ThinkPad X1", "price": 899}, {"id": 2, "name": "Dell XPS", "price": 949}]},
            ],
            "get_product_details": [
                {"id": 1, "name": "ThinkPad X1", "specs": "Intel i7, 16GB RAM, 512GB SSD", "in_stock": True}
            ],
            "add_to_cart": [{"success": True, "cart_total": 899}]
        },
        assertions=[
            {"after_turn": 0, "check": lambda r, calls: any(c["tool"] == "search_products" for c in calls), "name": "searches_for_products"},
            {"after_turn": 1, "check": lambda r, calls: "i7" in r.lower() or "ram" in r.lower(), "name": "includes_product_details"},
            {"after_turn": 2, "check": lambda r, calls: any(c["tool"] == "add_to_cart" for c in calls), "name": "adds_to_cart"},
        ]
    )

    result = await run_conversation_simulation(script)
    passed = sum(1 for a in result["assertion_results"] if a["passed"])
    total = len(result["assertion_results"])
    print(f"\n{script.name}: {passed}/{total} assertions passed")

asyncio.run(main())
```

---

## Option 5: Property-Based Simulation Testing

```python
import asyncio
import anthropic
import random
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class SimulationProperty:
    name: str
    description: str
    generate_input: callable  # generates random test input
    check: callable  # (input, response) -> bool
    n_samples: int = 5

async def test_property(prop: SimulationProperty, system: str) -> dict:
    """Run N random samples and check the property holds for each."""
    results = []
    for i in range(prop.n_samples):
        test_input = prop.generate_input()
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": test_input}]
        )
        output = resp.content[0].text
        passed = prop.check(test_input, output)
        results.append({"input": test_input[:50], "passed": passed, "output": output[:50]})

    pass_count = sum(1 for r in results if r["passed"])
    return {
        "property": prop.name,
        "passed": pass_count,
        "total": prop.n_samples,
        "pass_rate": pass_count / prop.n_samples,
        "failures": [r for r in results if not r["passed"]]
    }

SYSTEM = "You are a helpful assistant. Answer questions concisely."

PROPERTIES = [
    SimulationProperty(
        name="response_always_non_empty",
        description="Agent always returns non-empty response",
        generate_input=lambda: random.choice(["Hello", "What is 2+2?", "Tell me about Python", "Hi", "?"]),
        check=lambda inp, out: len(out.strip()) > 0,
        n_samples=10
    ),
    SimulationProperty(
        name="factual_questions_have_specific_answer",
        description="Factual questions get specific, not vague answers",
        generate_input=lambda: random.choice([
            "What is the capital of France?",
            "What is 5 * 8?",
            "How many days in a week?"
        ]),
        check=lambda inp, out: len(out.split()) >= 3,  # at minimum 3 words
        n_samples=5
    ),
    SimulationProperty(
        name="code_requests_include_code",
        description="Requests for code get responses containing code",
        generate_input=lambda: f"Write a Python function to {random.choice(['add two numbers', 'reverse a string', 'check if a number is prime'])}",
        check=lambda inp, out: "def " in out or "```" in out,
        n_samples=5
    ),
]

async def property_based_simulation():
    results = await asyncio.gather(*[test_property(p, SYSTEM) for p in PROPERTIES])
    print("PROPERTY-BASED SIMULATION RESULTS")
    print("=" * 50)
    for r in results:
        status = "✓" if r["pass_rate"] == 1.0 else ("⚠" if r["pass_rate"] >= 0.8 else "✗")
        print(f"{status} {r['property']}: {r['passed']}/{r['total']} ({r['pass_rate']:.0%})")
        if r["failures"]:
            for f in r["failures"][:2]:
                print(f"  Failure: input='{f['input']}' output='{f['output']}'")

asyncio.run(property_based_simulation())
```

---

## Option 6: Load and Concurrency Simulation

```python
import asyncio
import anthropic
import time
import statistics
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class LoadTestResult:
    total_requests: int
    completed: int
    failed: int
    latencies_ms: list[float]
    errors: list[str] = field(default_factory=list)

    def p50(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0

    def p95(self) -> float:
        if not self.latencies_ms:
            return 0
        sorted_l = sorted(self.latencies_ms)
        return sorted_l[int(len(sorted_l) * 0.95)]

    def p99(self) -> float:
        if not self.latencies_ms:
            return 0
        sorted_l = sorted(self.latencies_ms)
        return sorted_l[int(len(sorted_l) * 0.99)]

    def throughput_rps(self, duration_s: float) -> float:
        return self.completed / max(duration_s, 0.001)

async def simulate_single_request(question: str, tools: list[dict], tool_handler: callable) -> float:
    """Simulate one agent request. Returns latency in ms."""
    t0 = time.time()
    messages = [{"role": "user", "content": question}]
    for _ in range(5):  # max tool loops
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "end_turn":
            break
        if resp.stop_reason == "tool_use":
            results = [{"type": "tool_result", "tool_use_id": b.id, "content": str(tool_handler(b.name, b.input))}
                       for b in resp.content if b.type == "tool_use"]
            messages.append({"role": "user", "content": results})
    return (time.time() - t0) * 1000

async def concurrent_load_simulation(
    questions: list[str],
    concurrency: int,
    n_requests: int,
) -> LoadTestResult:
    tools = [{"name": "calculator", "description": "Calculate", "input_schema": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}}]

    def tool_handler(name: str, inp: dict):
        if name == "calculator":
            try:
                return {"result": eval(inp.get("expr", "0"))}
            except Exception:
                return {"error": "invalid expression"}
        return {"error": "unknown tool"}

    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors: list[str] = []
    completed = 0

    async def run_request(i: int):
        nonlocal completed
        question = questions[i % len(questions)]
        async with sem:
            try:
                latency = await simulate_single_request(question, tools, tool_handler)
                latencies.append(latency)
                completed += 1
            except Exception as e:
                errors.append(str(e))

    t_start = time.time()
    await asyncio.gather(*[run_request(i) for i in range(n_requests)], return_exceptions=True)
    duration = time.time() - t_start

    result = LoadTestResult(
        total_requests=n_requests,
        completed=completed,
        failed=n_requests - completed,
        latencies_ms=latencies,
        errors=errors[:5]
    )
    print(f"\nLoad Simulation Results (concurrency={concurrency}, n={n_requests})")
    print(f"  Completed: {result.completed}/{result.total_requests}")
    print(f"  Throughput: {result.throughput_rps(duration):.1f} RPS")
    print(f"  Latency p50={result.p50():.0f}ms p95={result.p95():.0f}ms p99={result.p99():.0f}ms")
    return result

async def main():
    questions = [
        "What is 15 + 27?",
        "Calculate 100 * 0.075",
        "What is Python? Answer in one sentence.",
    ]
    result = await concurrent_load_simulation(questions, concurrency=3, n_requests=6)
    assert result.completed > 0, "At least some requests should complete"
    assert result.p95() < 30000, "p95 latency should be under 30 seconds"
    print("✓ Load simulation assertions passed")

asyncio.run(main())
```

---

## Comparison

| Option | Test Type | Coverage | Realism | Best For |
|--------|-----------|---------|---------|----------|
| 1 – Scripted Tools | Integration | Tool calling flow | High | Verifying tool usage patterns |
| 2 – Scenario Suite | Integration | E2E scenarios | High | Regression testing |
| 3 – Adversarial | Failure modes | Error handling | High | Resilience testing |
| 4 – Multi-Turn | Conversation flow | Turn-by-turn | Very high | Conversational agents |
| 5 – Property-Based | Invariants | Random inputs | Medium | Behavior invariant verification |
| 6 – Load Simulation | Performance | Concurrency | High | Scalability validation |

**Recommendation:** Build your integration test suite starting with Option 2 (scenario-based) to cover your most important user journeys. Add Option 3 (adversarial) to test error handling and Option 4 (multi-turn) for conversational flows. Run Option 5 (property-based) in CI to catch edge cases before they reach production. Use Option 6 to validate performance under concurrent load before major releases.
