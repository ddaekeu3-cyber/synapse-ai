---
layout: solution
title: "Agent Doesn't Implement Dynamic Instruction Injection Based on Runtime Context"
category: prompt-engineering
description: "Automatically inject relevant instructions, constraints, and context snippets into the system prompt at runtime based on the current user, environment, task type, or tool availability."
tags: [prompt-engineering, dynamic-prompts, system-prompt, context-injection, personalization, runtime]
---

# Agent Doesn't Implement Dynamic Instruction Injection Based on Runtime Context

## Problem

A static system prompt written at deployment time cannot adapt to the wide variety of runtime situations an agent encounters: different users with different roles, different tools available in different environments, different task types requiring different constraints, or different operational modes (production vs. staging). Agents with static prompts either include too many instructions (bloating every request) or miss critical context-specific guidance, producing off-target responses.

## Solutions

### Option 1: User-Role-Based Instruction Injection

Build the system prompt by combining a base prompt with role-specific instruction blocks selected at request time.

```python
import anthropic

client = anthropic.Anthropic()

BASE_INSTRUCTIONS = """
You are a helpful AI assistant. Follow all instructions carefully.
Always be concise, accurate, and professional.
"""

ROLE_INSTRUCTIONS = {
    "admin": """
ADMIN MODE:
- You may discuss internal system configuration and architecture details.
- You can suggest database queries, infrastructure changes, and admin operations.
- Always warn about destructive operations before suggesting them.
""",
    "developer": """
DEVELOPER MODE:
- Focus on technical accuracy and code quality.
- Provide code examples in Python unless another language is specified.
- Suggest best practices, error handling, and testing approaches.
- Reference relevant documentation and libraries.
""",
    "analyst": """
ANALYST MODE:
- Prioritize data interpretation, trends, and statistical reasoning.
- Structure responses with clear sections: Summary, Analysis, Recommendations.
- Highlight confidence levels and data limitations.
- Avoid speculation beyond what the data supports.
""",
    "end_user": """
USER MODE:
- Use plain language; avoid technical jargon.
- Provide step-by-step instructions for any procedures.
- Offer to clarify anything that might be confusing.
- Do not discuss internal system details or implementation specifics.
""",
}

ENVIRONMENT_INSTRUCTIONS = {
    "production": "\nIMPORTANT: This is a PRODUCTION environment. Recommend caution for any irreversible actions.\n",
    "staging":    "\nNote: This is a STAGING environment. Experimental suggestions are acceptable.\n",
    "local":      "\nNote: This is a LOCAL development environment.\n",
}


def build_system_prompt(
    user_role: str,
    environment: str,
    extra_context: str = "",
) -> str:
    role_block = ROLE_INSTRUCTIONS.get(user_role, ROLE_INSTRUCTIONS["end_user"])
    env_block  = ENVIRONMENT_INSTRUCTIONS.get(environment, "")
    parts = [BASE_INSTRUCTIONS.strip(), role_block.strip(), env_block.strip()]
    if extra_context:
        parts.append(f"\nAdditional context:\n{extra_context}")
    return "\n\n".join(p for p in parts if p)


def chat(
    user_message: str,
    user_role: str = "end_user",
    environment: str = "production",
    extra_context: str = "",
) -> str:
    system = build_system_prompt(user_role, environment, extra_context)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    scenarios = [
        ("How do I reset the database?", "admin",     "production", ""),
        ("How do I reset the database?", "end_user",  "production", ""),
        ("How do I reset the database?", "developer", "staging",    "Using PostgreSQL 15"),
    ]
    for msg, role, env, ctx in scenarios:
        print(f"\nRole={role} Env={env}")
        print(f"Q: {msg}")
        reply = chat(msg, role, env, ctx)
        print(f"A: {reply[:200]}...")

# Expected Token Savings: Only role-relevant instructions included; removes irrelevant blocks
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Tool-Availability-Aware Instruction Injection

Dynamically inject instructions about available tools into the system prompt so the agent knows what it can and cannot do.

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

BASE_SYSTEM = "You are a capable AI assistant. Use available tools when helpful."

TOOL_INSTRUCTION_BLOCKS = {
    "web_search": "You have access to web_search. Use it to look up current information, news, and facts you are uncertain about.",
    "code_executor": "You have access to code_executor. You can run Python code to perform calculations, data analysis, and verify logic.",
    "file_manager": "You have access to file_manager. You can read and write files when the user needs file operations.",
    "calendar": "You have access to calendar. You can check and create calendar events for the user.",
    "email": "You have access to email. You can draft and send emails on behalf of the user.",
    "database": "You have access to database (read-only). You can query the user's data to answer specific questions.",
}

NO_TOOL_INSTRUCTION = (
    "You do NOT have access to any external tools in this session. "
    "Answer from your training knowledge only, and clearly state when you are uncertain."
)


@dataclass
class ToolConfig:
    name: str
    schema: dict


def build_system_with_tools(available_tools: list[str]) -> str:
    if not available_tools:
        return f"{BASE_SYSTEM}\n\n{NO_TOOL_INSTRUCTION}"

    tool_blocks = [
        TOOL_INSTRUCTION_BLOCKS[t]
        for t in available_tools
        if t in TOOL_INSTRUCTION_BLOCKS
    ]
    tool_section = "\n".join(f"- {b}" for b in tool_blocks)
    return (
        f"{BASE_SYSTEM}\n\n"
        f"Available tools this session:\n{tool_section}\n\n"
        "Only use tools when the user's request requires real-time data or file operations. "
        "For general knowledge questions, answer directly."
    )


def chat_with_dynamic_tools(
    user_message: str,
    available_tools: list[str],
    tool_schemas: list[dict] | None = None,
) -> str:
    system = build_system_with_tools(available_tools)
    kwargs = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
    }
    if tool_schemas:
        kwargs["tools"] = tool_schemas

    resp = client.messages.create(**kwargs)
    return resp.content[0].text if hasattr(resp.content[0], "text") else "(tool call)"


if __name__ == "__main__":
    msg = "What is the weather in Seoul today and can you save the result to a file?"
    for tools in [
        [],
        ["web_search"],
        ["web_search", "file_manager"],
    ]:
        print(f"\nAvailable tools: {tools or 'none'}")
        reply = chat_with_dynamic_tools(msg, tools)
        print(f"A: {reply[:200]}...")

# Expected Token Savings: Tool instructions only present when tools are available; no dead context
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Task-Type Classifier with Instruction Routing

Classify the incoming request into a task type, then inject the matching instruction block before calling the main model.

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

TASK_INSTRUCTIONS = {
    "code_generation": """
CODING TASK:
- Write clean, readable, well-commented code.
- Include error handling and edge case coverage.
- Add type hints for Python code.
- Provide a brief explanation of the approach.
""",
    "data_analysis": """
ANALYSIS TASK:
- Structure your response: Data Summary → Findings → Insights → Recommendations.
- Quantify observations where possible.
- Flag data quality issues or gaps.
- Distinguish correlation from causation.
""",
    "creative_writing": """
CREATIVE TASK:
- Prioritize originality and engagement over factual density.
- Match the tone and style requested by the user.
- Use vivid, specific language.
- Offer to iterate or adjust style if asked.
""",
    "factual_qa": """
FACTUAL Q&A TASK:
- Lead with the direct answer, then elaborate.
- Cite your confidence level for each claim.
- Flag anything that may have changed after your knowledge cutoff.
""",
    "planning": """
PLANNING TASK:
- Break the goal into numbered steps.
- Identify dependencies and risks.
- Estimate effort or duration where relevant.
- Offer a minimal viable plan alongside the full plan.
""",
    "general": """
GENERAL TASK:
- Be helpful, concise, and clear.
""",
}

BASE_SYSTEM = "You are a versatile AI assistant."


def classify_task(user_message: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"Classify this request into exactly one category: "
                "code_generation, data_analysis, creative_writing, factual_qa, planning, general.\n\n"
                f"Request: {user_message}\n\n"
                'Respond with JSON: {"task_type": "..."}'
            ),
        }],
    )
    raw = resp.content[0].text
    match = re.search(r'"task_type"\s*:\s*"(\w+)"', raw)
    if match:
        task = match.group(1)
        return task if task in TASK_INSTRUCTIONS else "general"
    return "general"


def routed_chat(user_message: str, verbose: bool = False) -> str:
    task_type = classify_task(user_message)
    instruction_block = TASK_INSTRUCTIONS[task_type]
    system = BASE_SYSTEM + "\n\n" + instruction_block.strip()

    if verbose:
        print(f"  [Classified as: {task_type}]")

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    requests = [
        "Write a Python function to merge two sorted lists.",
        "Analyze the trend: sales were 100, 120, 115, 130, 128, 140.",
        "Write a short poem about autumn rain.",
        "What year did World War II end?",
        "Help me plan a product launch for a new mobile app.",
    ]
    for req in requests:
        print(f"\nQ: {req}")
        reply = routed_chat(req, verbose=True)
        print(f"A: {reply[:200]}...")

# Expected Token Savings: 2 model calls per request but main call avoids irrelevant instructions
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Session-State-Aware Instruction Injection

Track session state (e.g., onboarding phase, confirmed preferences, active task) and inject instructions that reflect what the agent currently knows about the user.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

BASE_SYSTEM = "You are a personalized AI assistant."


@dataclass
class SessionState:
    user_name: str = ""
    language_preference: str = "en"
    expertise_level: str = "intermediate"   # beginner / intermediate / expert
    active_project: str = ""
    confirmed_preferences: dict = field(default_factory=dict)
    turn_count: int = 0

    def to_instruction_block(self) -> str:
        lines = []
        if self.user_name:
            lines.append(f"The user's name is {self.user_name}. Address them by name occasionally.")
        level_instructions = {
            "beginner":     "Use simple language; avoid jargon; explain every term.",
            "intermediate": "Use standard technical language; brief explanations are fine.",
            "expert":       "Use precise technical language; skip basics; go deep.",
        }
        lines.append(level_instructions.get(self.expertise_level, ""))
        if self.language_preference != "en":
            lines.append(f"The user prefers responses in: {self.language_preference}.")
        if self.active_project:
            lines.append(f"Active project context: {self.active_project}")
        if self.confirmed_preferences:
            pref_text = "; ".join(f"{k}={v}" for k, v in self.confirmed_preferences.items())
            lines.append(f"Known preferences: {pref_text}")
        if self.turn_count == 0:
            lines.append("This is the first message of the session. Briefly introduce yourself.")
        return "\n".join(lines)


def build_system(state: SessionState) -> str:
    block = state.to_instruction_block()
    return f"{BASE_SYSTEM}\n\n{block}" if block else BASE_SYSTEM


def chat(state: SessionState, user_message: str) -> tuple[str, SessionState]:
    system = build_system(state)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    state.turn_count += 1
    return resp.content[0].text, state


if __name__ == "__main__":
    # beginner user, first session
    state = SessionState(
        user_name="Alex",
        expertise_level="beginner",
        active_project="Building first web app with Flask",
    )
    turns = [
        "How do I install Flask?",
        "What is a virtual environment and do I need one?",
        "Can you show me a simple route?",
    ]
    for turn in turns:
        print(f"\nQ: {turn}")
        reply, state = chat(state, turn)
        print(f"A: {reply[:200]}...")
        print(f"  [Turn {state.turn_count} | expertise={state.expertise_level}]")

    # now switch to expert session
    print("\n--- Expert session ---")
    expert_state = SessionState(
        user_name="Dr. Kim",
        expertise_level="expert",
        active_project="Optimizing asyncio event loop performance",
        confirmed_preferences={"format": "code-first", "verbosity": "minimal"},
    )
    reply, expert_state = chat(expert_state, "How do I profile coroutine CPU time in asyncio?")
    print(f"Q: How do I profile coroutine CPU time in asyncio?")
    print(f"A: {reply[:300]}...")

# Expected Token Savings: Instructions tightly matched to user — no wasted generic guidance
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Feature-Flag-Driven Instruction Injection

Use a feature flag system to enable/disable instruction blocks at runtime without redeploying, enabling A/B testing and gradual rollouts.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# Feature flag store (in production: LaunchDarkly, Unleash, custom service)
FEATURE_FLAGS: dict[str, dict] = {
    "verbose_citations":     {"enabled": True,  "rollout_pct": 100},
    "safety_reminders":      {"enabled": True,  "rollout_pct": 50},
    "experimental_cot":      {"enabled": False, "rollout_pct": 0},
    "concise_mode":          {"enabled": True,  "rollout_pct": 30},
    "pro_user_features":     {"enabled": True,  "rollout_pct": 100},
}

INSTRUCTION_BLOCKS: dict[str, str] = {
    "verbose_citations":  "When making factual claims, cite your reasoning or note your confidence level.",
    "safety_reminders":   "Before suggesting any irreversible action, remind the user to back up their data.",
    "experimental_cot":   "Think step by step before answering. Show your reasoning process explicitly.",
    "concise_mode":       "Be extremely concise. Prefer bullet points over prose. No preamble.",
    "pro_user_features":  "You may discuss advanced configuration options and internal system details.",
}

BASE_SYSTEM = "You are a helpful AI assistant."


def is_flag_enabled(flag_name: str, user_id: str) -> bool:
    flag = FEATURE_FLAGS.get(flag_name, {"enabled": False, "rollout_pct": 0})
    if not flag["enabled"]:
        return False
    # deterministic hash-based rollout
    bucket = hash(f"{flag_name}:{user_id}") % 100
    return bucket < flag["rollout_pct"]


def build_system_for_user(user_id: str, user_tier: str = "free") -> tuple[str, list[str]]:
    active_flags: list[str] = []
    blocks: list[str] = [BASE_SYSTEM]

    for flag_name, instruction in INSTRUCTION_BLOCKS.items():
        # pro_user_features only for paid users
        if flag_name == "pro_user_features" and user_tier == "free":
            continue
        if is_flag_enabled(flag_name, user_id):
            blocks.append(instruction)
            active_flags.append(flag_name)

    return "\n\n".join(blocks), active_flags


def chat(user_id: str, user_tier: str, user_message: str) -> dict:
    system, active_flags = build_system_for_user(user_id, user_tier)

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return {
        "reply":        resp.content[0].text,
        "active_flags": active_flags,
        "user_id":      user_id,
    }


if __name__ == "__main__":
    users = [
        ("user_001", "pro"),
        ("user_002", "free"),
        ("user_003", "pro"),
    ]
    message = "How do I configure rate limiting for my API?"
    for uid, tier in users:
        result = chat(uid, tier, message)
        print(f"\nUser={uid} Tier={tier}")
        print(f"Flags: {result['active_flags']}")
        print(f"A: {result['reply'][:150]}...")

# Expected Token Savings: Only enabled instruction blocks included; dead flags consume zero tokens
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Async Context Pipeline with Composable Middleware

Build a composable pipeline of async middleware functions that each contribute instruction fragments to the final system prompt.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()


@dataclass
class RequestContext:
    user_id: str
    user_role: str
    environment: str
    tool_names: list[str]
    session_turn: int
    raw_message: str
    extra: dict = field(default_factory=dict)


@dataclass
class PromptBuilder:
    base: str = "You are a helpful AI assistant."
    blocks: list[str] = field(default_factory=list)

    def add(self, block: str) -> None:
        if block.strip():
            self.blocks.append(block.strip())

    def build(self) -> str:
        return "\n\n".join([self.base] + self.blocks)


Middleware = Callable[[RequestContext, PromptBuilder], Awaitable[None]]


class DynamicInstructionPipeline:
    def __init__(self) -> None:
        self._middlewares: list[Middleware] = []

    def use(self, mw: Middleware) -> None:
        self._middlewares.append(mw)

    async def build_prompt(self, ctx: RequestContext) -> str:
        builder = PromptBuilder()
        for mw in self._middlewares:
            await mw(ctx, builder)
        return builder.build()


# --- middleware definitions ---

async def env_middleware(ctx: RequestContext, builder: PromptBuilder) -> None:
    if ctx.environment == "production":
        builder.add("PRODUCTION environment: recommend caution for irreversible actions.")
    elif ctx.environment == "staging":
        builder.add("STAGING environment: experimental suggestions are acceptable.")


async def role_middleware(ctx: RequestContext, builder: PromptBuilder) -> None:
    role_map = {
        "admin":     "You may discuss internal architecture and admin operations.",
        "developer": "Focus on code quality, type safety, and testing best practices.",
        "analyst":   "Prioritize data-driven reasoning and structured findings.",
    }
    block = role_map.get(ctx.user_role, "")
    builder.add(block)


async def tools_middleware(ctx: RequestContext, builder: PromptBuilder) -> None:
    if not ctx.tool_names:
        builder.add("No external tools available. Answer from knowledge only.")
        return
    tool_list = ", ".join(ctx.tool_names)
    builder.add(f"Available tools: {tool_list}. Use them when the request requires real-time data.")


async def onboarding_middleware(ctx: RequestContext, builder: PromptBuilder) -> None:
    if ctx.session_turn == 0:
        builder.add("This is the first message. Greet the user warmly and briefly state your capabilities.")


async def length_middleware(ctx: RequestContext, builder: PromptBuilder) -> None:
    if len(ctx.raw_message) < 30:
        builder.add("The user sent a short message. Keep your response brief and ask a clarifying question if needed.")
    elif len(ctx.raw_message) > 500:
        builder.add("The user sent a detailed message. Match their level of detail in your response.")


# --- assemble pipeline ---
pipeline = DynamicInstructionPipeline()
pipeline.use(env_middleware)
pipeline.use(role_middleware)
pipeline.use(tools_middleware)
pipeline.use(onboarding_middleware)
pipeline.use(length_middleware)


async def dynamic_chat(ctx: RequestContext) -> dict:
    system = await pipeline.build_prompt(ctx)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": ctx.raw_message}],
    )
    return {
        "user_id": ctx.user_id,
        "system_len": len(system),
        "reply": resp.content[0].text,
    }


async def main() -> None:
    contexts = [
        RequestContext("u1", "admin",     "production", [],               0, "Hi!"),
        RequestContext("u2", "developer", "staging",    ["code_executor"], 3, "How do I implement a binary search tree?"),
        RequestContext("u3", "analyst",   "production", ["database"],       1, "Show me the sales trend for Q1." * 20),
    ]
    results = await asyncio.gather(*[dynamic_chat(ctx) for ctx in contexts])
    for r in results:
        print(f"\nUser={r['user_id']} system_prompt_chars={r['system_len']}")
        print(f"A: {r['reply'][:150]}...")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Composable middleware adds only relevant blocks; easily extended without bloat
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Injection Basis | Dynamic | Overhead | Extensibility | Best For |
|--------|----------------|---------|----------|---------------|----------|
| 1 | User role + environment | Yes | None | Medium | Multi-role platforms |
| 2 | Tool availability | Yes | None | Medium | Tool-use agents |
| 3 | Task type classifier | Yes | 1 small LLM call | Low | General-purpose agents |
| 4 | Session state | Yes | None | High | Personalized assistants |
| 5 | Feature flags | Yes | Hash lookup | Very High | A/B testing, gradual rollout |
| 6 | Async middleware pipeline | Yes | None (async) | Highest | Production systems needing composability |
