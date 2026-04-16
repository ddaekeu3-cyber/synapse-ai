---
title: "Agent Doesn't Implement Agent Orchestration Topology Selection"
description: "Solutions for choosing the right multi-agent topology — sequential chain, parallel fan-out, hierarchical tree, graph-based, or dynamic routing — based on task structure and constraints."
tags: [general, orchestration, multi-agent, topology, architecture]
difficulty: advanced
---

## Problem

Multi-agent systems default to a single topology (usually sequential chain) regardless of whether the task is parallelizable, hierarchical, or dynamic. A sequential chain wastes latency on independent subtasks. A flat parallel topology loses context flow between dependent steps. Without topology selection, agents either run slower than necessary or produce incoherent results.

---

## Solution 1: Sequential Chain — Dependent Step Pipeline

Use a linear chain when each step depends on the previous output. Optimal for tasks where context must flow through the entire pipeline.

```python
import anthropic
from dataclasses import dataclass
from typing import Callable, Optional

client = anthropic.Anthropic()

@dataclass
class ChainStep:
    name: str
    system_prompt: str
    input_transform: Optional[Callable[[str], str]] = None
    output_transform: Optional[Callable[[str], str]] = None

def run_sequential_chain(
    initial_input: str,
    steps: list[ChainStep],
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    context = initial_input
    step_outputs = []

    for step in steps:
        # Apply input transform if defined
        step_input = step.input_transform(context) if step.input_transform else context

        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=step.system_prompt,
            messages=[{"role": "user", "content": step_input}],
        )
        output = response.content[0].text

        # Apply output transform if defined
        context = step.output_transform(output) if step.output_transform else output
        step_outputs.append({"step": step.name, "output": output[:100]})

    return {"topology": "sequential-chain", "steps": step_outputs, "final": context}

# Example: document processing pipeline
steps = [
    ChainStep(
        name="extract",
        system_prompt="Extract the key facts from the input as a bulleted list.",
    ),
    ChainStep(
        name="analyze",
        system_prompt="Analyze the key facts and identify the most important insight.",
    ),
    ChainStep(
        name="summarize",
        system_prompt="Write a one-sentence executive summary of the analysis.",
    ),
]

result = run_sequential_chain(
    "The quarterly report shows revenue grew 23% YoY to $4.2B. "
    "Operating margins expanded 200bps to 18%. R&D spend increased 40% "
    "as the company accelerated investment in AI products. Customer "
    "retention improved to 94% from 91% last year.",
    steps=steps,
)

print(f"Topology: {result['topology']}")
for s in result["steps"]:
    print(f"  [{s['step']}]: {s['output']}")
print(f"Final: {result['final']}")
```

---

## Solution 2: Parallel Fan-Out with Result Aggregation

Use parallel execution when subtasks are independent. Cuts wall-clock time from O(n) to O(1) for n independent tasks.

```python
import anthropic
import asyncio
from dataclasses import dataclass
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class ParallelTask:
    name: str
    prompt: str
    system: str = ""
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 512

async def run_parallel_task(task: ParallelTask) -> dict:
    kwargs = {"system": task.system} if task.system else {}
    response = await client.messages.create(
        model=task.model,
        max_tokens=task.max_tokens,
        messages=[{"role": "user", "content": task.prompt}],
        **kwargs,
    )
    return {"name": task.name, "output": response.content[0].text}

async def run_parallel_fanout(
    tasks: list[ParallelTask],
    aggregator_prompt: str,
    aggregator_model: str = "claude-sonnet-4-6",
) -> dict:
    # Run all tasks concurrently
    results = await asyncio.gather(*[run_parallel_task(t) for t in tasks])

    # Aggregate results
    combined = "\n\n".join([
        f"[{r['name']}]\n{r['output']}" for r in results
    ])
    agg_response = await client.messages.create(
        model=aggregator_model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"{aggregator_prompt}\n\n{combined}",
        }],
    )

    return {
        "topology": "parallel-fanout",
        "parallel_results": results,
        "aggregated": agg_response.content[0].text,
        "parallelism": len(tasks),
    }

async def main():
    topic = "the impact of large language models on software engineering"
    tasks = [
        ParallelTask(
            name="technical-analysis",
            prompt=f"Analyze the technical implications of {topic}.",
            system="You are a software engineer. Be technical and specific.",
        ),
        ParallelTask(
            name="business-impact",
            prompt=f"Analyze the business and economic impact of {topic}.",
            system="You are a business analyst. Focus on ROI and market dynamics.",
        ),
        ParallelTask(
            name="risks-challenges",
            prompt=f"Identify the risks and challenges related to {topic}.",
            system="You are a risk analyst. Be thorough about downsides.",
        ),
        ParallelTask(
            name="future-outlook",
            prompt=f"What is the 3-5 year outlook for {topic}?",
            system="You are a technology futurist. Be forward-looking.",
        ),
    ]

    result = await run_parallel_fanout(
        tasks,
        aggregator_prompt="Synthesize these expert perspectives into a balanced executive summary:",
    )

    print(f"Topology: {result['topology']} ({result['parallelism']} parallel agents)")
    for r in result["parallel_results"]:
        print(f"  [{r['name']}]: {r['output'][:60]}...")
    print(f"\nSynthesis: {result['aggregated'][:200]}...")

asyncio.run(main())
```

---

## Solution 3: Hierarchical Tree — Orchestrator + Specialists

Use a tree topology when a task decomposes into specialized subtasks that require domain expertise. The orchestrator delegates to specialists who may themselves delegate further.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class AgentNode:
    name: str
    role: str
    system_prompt: str
    children: list["AgentNode"] = field(default_factory=list)
    model: str = "claude-haiku-4-5-20251001"

    def run(self, task: str, depth: int = 0) -> dict:
        indent = "  " * depth
        print(f"{indent}[{self.name}] Processing: {task[:50]}...")

        # First, run children if any
        child_outputs = {}
        for child in self.children:
            child_task = f"As the {child.role}, address this aspect: {task}"
            child_outputs[child.name] = child.run(child_task, depth + 1)

        # Then run own reasoning, informed by children's output
        context = task
        if child_outputs:
            child_summaries = "\n".join([
                f"[{name}]: {out['output'][:100]}"
                for name, out in child_outputs.items()
            ])
            context = f"{task}\n\nExpert inputs:\n{child_summaries}"

        response = client.messages.create(
            model=self.model,
            max_tokens=512,
            system=self.system_prompt,
            messages=[{"role": "user", "content": context}],
        )
        output = response.content[0].text

        return {
            "agent": self.name,
            "output": output,
            "depth": depth,
            "children": child_outputs,
        }

# Build tree
tree = AgentNode(
    name="orchestrator",
    role="project lead",
    system_prompt="You are a project lead. Synthesize expert inputs into a cohesive recommendation.",
    model="claude-sonnet-4-6",
    children=[
        AgentNode(
            name="tech-lead",
            role="technical expert",
            system_prompt="You are a technical expert. Assess feasibility and implementation complexity.",
            children=[
                AgentNode("backend", "backend engineer", "Evaluate server-side requirements."),
                AgentNode("ml-engineer", "ML engineer", "Evaluate ML/AI components needed."),
            ],
        ),
        AgentNode(
            name="product-manager",
            role="product expert",
            system_prompt="You are a product manager. Assess user value and priority.",
        ),
        AgentNode(
            name="security-expert",
            role="security reviewer",
            system_prompt="You are a security expert. Identify security risks.",
        ),
    ],
)

task = "Should we build a real-time AI code review feature that analyzes pull requests automatically?"
result = tree.run(task)
print(f"\n=== Final Recommendation ===\n{result['output']}")
```

---

## Solution 4: Task Graph with Dependency Resolution

Model the task as a DAG (directed acyclic graph) where nodes are subtasks and edges are dependencies. Execute nodes as soon as their dependencies are satisfied.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class TaskNode:
    task_id: str
    prompt_template: str  # can reference {dep_id} outputs
    dependencies: list[str] = field(default_factory=list)
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 512

class TaskGraph:
    def __init__(self, nodes: list[TaskNode]):
        self._nodes = {n.task_id: n for n in nodes}
        self._results: dict[str, str] = {}
        self._locks: dict[str, asyncio.Event] = {n.task_id: asyncio.Event() for n in nodes}

    async def _run_node(self, node: TaskNode):
        # Wait for all dependencies
        for dep_id in node.dependencies:
            await self._locks[dep_id].wait()

        # Build prompt using dependency outputs
        prompt = node.prompt_template
        for dep_id in node.dependencies:
            dep_output = self._results.get(dep_id, "")
            prompt = prompt.replace(f"{{{dep_id}}}", dep_output)

        response = await client.messages.create(
            model=node.model,
            max_tokens=node.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        self._results[node.task_id] = response.content[0].text
        self._locks[node.task_id].set()
        print(f"  [✓] {node.task_id} completed")

    async def run(self) -> dict[str, str]:
        print(f"Running task graph ({len(self._nodes)} nodes)...")
        await asyncio.gather(*[self._run_node(n) for n in self._nodes.values()])
        return self._results

async def main():
    # Task graph for a research report:
    # fetch_data ─┐
    # fetch_sources ─┤─→ analyze → draft → review → final
    # background ─┘

    nodes = [
        TaskNode("fetch_data",    "Summarize key statistics about AI adoption in enterprise software.", []),
        TaskNode("fetch_sources", "List 3 recent authoritative sources about enterprise AI trends.", []),
        TaskNode("background",    "Provide historical context on AI adoption from 2020-2023.", []),
        TaskNode(
            "analyze",
            "Analyze these findings:\nData: {fetch_data}\nSources: {fetch_sources}\n"
            "Background: {background}\n\nWhat are the 3 key insights?",
            ["fetch_data", "fetch_sources", "background"],
        ),
        TaskNode(
            "draft",
            "Write a 3-paragraph report section based on this analysis:\n{analyze}",
            ["analyze"],
            model="claude-sonnet-4-6",
        ),
        TaskNode(
            "review",
            "Review this draft for accuracy and clarity. Provide specific improvements:\n{draft}",
            ["draft"],
        ),
        TaskNode(
            "final",
            "Apply these improvements to produce the final version:\nDraft: {draft}\nImprovements: {review}",
            ["draft", "review"],
            model="claude-sonnet-4-6",
        ),
    ]

    graph = TaskGraph(nodes)
    results = await graph.run()

    print(f"\nTopology: task-graph ({len(nodes)} nodes)")
    print(f"\nFinal report:\n{results['final'][:300]}...")

asyncio.run(main())
```

---

## Solution 5: Dynamic Router — Topology Selection Based on Task Analysis

Analyze the incoming task and automatically select the best topology: chain, parallel, hierarchical, or hybrid.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Literal

client = anthropic.Anthropic()

TopologyType = Literal["sequential", "parallel", "hierarchical", "hybrid"]

TOPOLOGY_SELECTOR_PROMPT = """Analyze this task and determine the optimal multi-agent topology.

Task: {task}

Topologies:
- sequential: steps must happen in order, each depends on previous
- parallel: independent subtasks that can run simultaneously
- hierarchical: needs specialist sub-agents under an orchestrator
- hybrid: combination (e.g., parallel collection then sequential processing)

Respond ONLY with JSON:
{{
  "topology": "sequential|parallel|hierarchical|hybrid",
  "reasoning": "brief explanation",
  "estimated_agents": 2-8,
  "parallel_opportunities": ["list of steps that can be parallelized"],
  "sequential_dependencies": ["list of dependency constraints"],
  "specialist_roles_needed": ["list of specialist roles if hierarchical"]
}}"""

@dataclass
class TopologyPlan:
    topology: TopologyType
    reasoning: str
    estimated_agents: int
    parallel_opportunities: list[str]
    sequential_dependencies: list[str]
    specialist_roles: list[str]

def select_topology(task: str) -> TopologyPlan:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": TOPOLOGY_SELECTOR_PROMPT.format(task=task),
        }],
    )
    try:
        data = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        data = {"topology": "sequential", "reasoning": "Default", "estimated_agents": 2,
                "parallel_opportunities": [], "sequential_dependencies": [],
                "specialist_roles_needed": []}

    return TopologyPlan(
        topology=data.get("topology", "sequential"),
        reasoning=data.get("reasoning", ""),
        estimated_agents=data.get("estimated_agents", 2),
        parallel_opportunities=data.get("parallel_opportunities", []),
        sequential_dependencies=data.get("sequential_dependencies", []),
        specialist_roles=data.get("specialist_roles_needed", []),
    )

def execute_with_topology(task: str, plan: TopologyPlan) -> str:
    """Execute task using the selected topology (simplified)."""
    if plan.topology == "sequential":
        steps = ["Step 1: " + task, "Step 2: Refine", "Step 3: Finalize"]
        context = task
        for step in steps:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=256,
                messages=[{"role": "user", "content": f"{step}\nContext: {context}"}]
            )
            context = resp.content[0].text
        return context

    elif plan.topology == "parallel":
        perspectives = [f"Analyze from perspective {i+1}" for i in range(min(3, plan.estimated_agents))]
        outputs = []
        for p in perspectives:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=256,
                messages=[{"role": "user", "content": f"{p}: {task}"}]
            )
            outputs.append(resp.content[0].text)

        combined = "\n".join(outputs)
        final = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            messages=[{"role": "user", "content": f"Synthesize:\n{combined}"}]
        )
        return final.content[0].text

    else:
        # Default for hierarchical/hybrid
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512,
            messages=[{"role": "user", "content": task}]
        )
        return resp.content[0].text

# Test with different task types
tasks = [
    "Write a step-by-step tutorial for deploying a Python app to AWS ECS.",
    "Research and compare GPT-4, Claude 3, and Gemini across 5 dimensions.",
    "Design a complete authentication system for a multi-tenant SaaS application.",
    "Fix this Python bug: TypeError: 'NoneType' object is not subscriptable at line 42.",
]

for task in tasks:
    plan = select_topology(task)
    print(f"\nTask: {task[:60]}...")
    print(f"  → Topology: {plan.topology} ({plan.estimated_agents} agents)")
    print(f"  → Reasoning: {plan.reasoning[:80]}")
    if plan.parallel_opportunities:
        print(f"  → Parallel: {plan.parallel_opportunities[:2]}")
    if plan.specialist_roles:
        print(f"  → Specialists: {plan.specialist_roles[:3]}")
```

---

## Solution 6: Adaptive Topology with Mid-Execution Restructuring

Monitor execution progress and restructure the topology mid-run if performance or quality signals indicate the initial choice was wrong.

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class ExecutionMetrics:
    latency_ms: int
    output_quality_score: float  # 0-1, self-assessed
    subtask_count: int
    dependencies_blocked: int  # subtasks blocked waiting for deps

@dataclass
class AdaptiveOrchestrator:
    task: str
    topology: str = "sequential"
    max_agents: int = 5
    quality_threshold: float = 0.7
    _history: list[dict] = field(default_factory=list)

    def _assess_quality(self, output: str) -> float:
        """Use a fast model to self-assess output quality."""
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=64,
            messages=[{
                "role": "user",
                "content": f"Rate this output quality 0.0-1.0 (JSON only): {output[:200]}"
            }]
        )
        import re
        match = re.search(r"0?\.\d+|[01]\.0", resp.content[0].text)
        return float(match.group()) if match else 0.5

    def _should_restructure(self, metrics: ExecutionMetrics) -> Optional[str]:
        if metrics.latency_ms > 5000 and self.topology == "sequential":
            return "parallel"  # Too slow — switch to parallel
        if metrics.output_quality_score < self.quality_threshold and self.topology == "parallel":
            return "hierarchical"  # Quality poor — need specialist hierarchy
        if metrics.dependencies_blocked > metrics.subtask_count // 2:
            return "sequential"  # Too many blocked deps — go sequential
        return None

    def run(self) -> dict:
        steps_completed = 0
        topology_switches = []
        final_output = ""

        for attempt in range(3):  # Max 3 topology switches
            t0 = time.time()
            print(f"[Orchestrator] Running with topology: {self.topology}")

            if self.topology == "sequential":
                subtasks = [
                    f"Step {i+1}: " + self.task
                    for i in range(min(3, self.max_agents))
                ]
                context = self.task
                for subtask in subtasks:
                    resp = client.messages.create(
                        model="claude-haiku-4-5-20251001", max_tokens=256,
                        messages=[{"role": "user", "content": f"{subtask}\nContext: {context}"}]
                    )
                    context = resp.content[0].text
                    steps_completed += 1
                final_output = context

            elif self.topology == "parallel":
                angles = ["technical", "business", "user experience"]
                outputs = []
                for angle in angles[:self.max_agents]:
                    resp = client.messages.create(
                        model="claude-haiku-4-5-20251001", max_tokens=256,
                        messages=[{"role": "user", "content": f"From {angle} perspective: {self.task}"}]
                    )
                    outputs.append(resp.content[0].text)
                    steps_completed += 1
                merge = client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=256,
                    messages=[{"role": "user", "content": "Synthesize: " + "\n".join(outputs)}]
                )
                final_output = merge.content[0].text

            else:  # hierarchical
                resp = client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=512,
                    messages=[{"role": "user", "content": f"As an expert team lead, address: {self.task}"}]
                )
                final_output = resp.content[0].text
                steps_completed += 1

            latency_ms = int((time.time() - t0) * 1000)
            quality = self._assess_quality(final_output)
            metrics = ExecutionMetrics(
                latency_ms=latency_ms,
                output_quality_score=quality,
                subtask_count=steps_completed,
                dependencies_blocked=0,
            )

            new_topology = self._should_restructure(metrics)
            if new_topology and new_topology != self.topology:
                topology_switches.append(f"{self.topology}→{new_topology}")
                self.topology = new_topology
                print(f"  [Restructure] Switching to {new_topology}: quality={quality:.2f}, latency={latency_ms}ms")
            else:
                break  # Satisfactory — stop

        return {
            "topology": "adaptive",
            "final_topology": self.topology,
            "topology_switches": topology_switches,
            "output": final_output,
            "steps_completed": steps_completed,
        }

# Run adaptive orchestrator
task = "Analyze the pros and cons of microservices vs monolithic architecture for a startup."
orchestrator = AdaptiveOrchestrator(task=task, topology="sequential", max_agents=4)
result = orchestrator.run()

print(f"\nFinal topology: {result['final_topology']}")
print(f"Topology switches: {result['topology_switches']}")
print(f"Output: {result['output'][:200]}...")
```

---

## Comparison

| Topology | Best For | Latency | Output Coherence | Complexity | When to Use |
|---|---|---|---|---|---|
| Sequential Chain | Dependent pipeline steps | O(n) | High (full context flow) | Low | Refinement loops, transformations |
| Parallel Fan-Out | Independent subtasks | O(1) | Medium (aggregation needed) | Low | Research, multi-perspective analysis |
| Hierarchical Tree | Specialized domain expertise | O(depth) | High | Medium | Complex decisions, multi-domain tasks |
| Task Graph (DAG) | Mixed dependencies | O(critical-path) | High | High | Large decomposable tasks |
| Dynamic Router | Unknown task structure | O(1) + routing | Depends on selected | Medium | General-purpose orchestrators |
| Adaptive Topology | Uncertain, evolving tasks | Variable | Adaptive | High | Production systems with quality SLOs |

**Recommended approach:** Start with Solution 5 (dynamic router) that selects topology per-task, implement Solutions 1 and 2 as the core execution backends, and add Solution 6 (adaptive) only when quality SLOs require mid-execution correction.
