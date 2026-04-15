#!/usr/bin/env python3
"""Generate category index pages for each solution category."""
import os

CATEGORIES = {
    "auth": {
        "title": "Auth Errors",
        "description": "Solutions for 401, 403, OAuth refresh failures, JWT validation errors, API key issues, and authentication loops in AI agents.",
        "guide": "/synapse-ai/guide/auth-errors",
        "guide_title": "Auth Error Guide",
    },
    "openclaw": {
        "title": "OpenClaw Errors",
        "description": "Solutions for OpenClaw gateway failures, skill errors, session disconnects, channel issues, and agent startup problems.",
        "guide": "/synapse-ai/guide/openclaw-errors",
        "guide_title": "OpenClaw Error Guide",
    },
    "general": {
        "title": "General Agent Errors",
        "description": "Solutions for general AI agent errors that don't fit a specific category — runtime failures, unexpected behavior, and cross-cutting issues.",
        "guide": None,
        "guide_title": None,
    },
    "docker": {
        "title": "Docker & Sandbox Errors",
        "description": "Solutions for Docker container errors in AI agent deployments: EACCES permission errors, networking failures, OOM kills, and volume mount issues.",
        "guide": "/synapse-ai/guide/docker-errors",
        "guide_title": "Docker Error Guide",
    },
    "config": {
        "title": "Configuration Errors",
        "description": "Solutions for AI agent configuration errors: missing env vars, YAML syntax issues, wrong model IDs, secrets management, and startup failures.",
        "guide": "/synapse-ai/guide/config-errors",
        "guide_title": "Config Error Guide",
    },
    "loop-stuck": {
        "title": "Loop & Stuck Agent Errors",
        "description": "Solutions for infinite loops, retry storms, stuck agents, circuit breaker failures, and exec storms in AI agents.",
        "guide": "/synapse-ai/guide/loop-stuck-errors",
        "guide_title": "Loop & Stuck Error Guide",
    },
    "performance": {
        "title": "Performance Errors",
        "description": "Solutions for AI agent performance problems: latency spikes, slow tool calls, context bloat, cold starts, and throughput bottlenecks.",
        "guide": "/synapse-ai/guide/performance-errors",
        "guide_title": "Performance Error Guide",
    },
    "context-window": {
        "title": "Context Window Errors",
        "description": "Solutions for context window overflow, truncation, context budget exceeded errors, and long-context management in AI agents.",
        "guide": "/synapse-ai/guide/context-window-errors",
        "guide_title": "Context Window Error Guide",
    },
    "hallucination": {
        "title": "Hallucination Errors",
        "description": "Solutions for AI agent hallucinations: fabricated outputs, wrong facts, invented functions, confabulated decisions, and verification failures.",
        "guide": "/synapse-ai/guide/hallucination-prevention",
        "guide_title": "Hallucination Prevention Guide",
    },
    "memory": {
        "title": "Memory & Session Errors",
        "description": "Solutions for AI agent memory loss, session amnesia, context not persisting across restarts, and cross-session state management.",
        "guide": "/synapse-ai/guide/memory-session-errors",
        "guide_title": "Memory & Session Error Guide",
    },
    "telegram": {
        "title": "Telegram Bot Errors",
        "description": "Solutions for Telegram bot errors with AI agents: 429 rate limits, webhook failures, session splits, silent hangs, and dual response bugs.",
        "guide": None,
        "guide_title": None,
    },
    "rate-limit": {
        "title": "Rate Limit Errors",
        "description": "Solutions for 429 Too Many Requests errors, API throttling, quota exhaustion, and rate limit recovery patterns for AI agents.",
        "guide": "/synapse-ai/guide/rate-limit-errors",
        "guide_title": "Rate Limit Error Guide",
    },
    "token-cost": {
        "title": "Token Cost & Waste Errors",
        "description": "Solutions for excessive token usage, billing spikes, token waste patterns, and cost optimization for AI agent deployments.",
        "guide": "/synapse-ai/guide/token-saving",
        "guide_title": "Token Saving Guide",
    },
    "tool-failure": {
        "title": "Tool & MCP Failure Errors",
        "description": "Solutions for MCP tool failures, function call errors, schema mismatches, plugin crashes, and tool timeout issues in AI agents.",
        "guide": "/synapse-ai/guide/tool-failure-errors",
        "guide_title": "Tool Failure Error Guide",
    },
    "prompt-engineering": {
        "title": "Prompt Engineering Errors",
        "description": "Solutions for prompt injection, instruction failures, role confusion, output format errors, sycophancy, and system prompt drift in AI agents.",
        "guide": "/synapse-ai/guide/prompt-engineering",
        "guide_title": "Prompt Engineering Guide",
    },
    "concurrency": {
        "title": "Concurrency Errors",
        "description": "Solutions for race conditions, deadlocks, session interleaving, duplicate message processing, and async errors in AI agents.",
        "guide": "/synapse-ai/guide/concurrency-errors",
        "guide_title": "Concurrency Error Guide",
    },
}

TEMPLATE = """\
---
layout: default
title: "TITLE — AI Agent Solutions"
description: "DESCRIPTION"
permalink: /solutions/SLUG/
---

# TITLE

DESCRIPTION

{%- assign cat_solutions = site.solutions | where: "category", "SLUG" | sort: "title" -%}
{% if cat_solutions.size == 0 %}
<p style="color: var(--text-muted)">Solutions loading...</p>
{% else %}
<p style="color: var(--text-muted); margin-bottom: 24px;">{{ cat_solutions.size }} solutions in this category</p>

<ul class="solution-list">
{% for solution in cat_solutions %}
  <li>
    <a href="{{ solution.url | prepend: site.baseurl }}">{{ solution.title }}</a>
    {% if solution.description %}
    <br><span style="color: var(--text-muted); font-size: 0.88rem;">{{ solution.description | truncate: 120 }}</span>
    {% endif %}
  </li>
{% endfor %}
</ul>
{% endif %}
GUIDE_SECTION
---

[← All solutions](/synapse-ai/) | [Browse all guides](/synapse-ai/guide/)
"""

GUIDE_SECTION = """
---

## Related Guide

The **[GUIDE_TITLE](GUIDE_URL)** covers root causes, prevention patterns, and checklists for this category of errors.
"""

def main():
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "solutions")
    os.makedirs(output_dir, exist_ok=True)

    for slug, meta in CATEGORIES.items():
        cat_dir = os.path.join(output_dir, slug)
        os.makedirs(cat_dir, exist_ok=True)

        guide_section = ""
        if meta["guide"]:
            guide_section = GUIDE_SECTION.replace("GUIDE_TITLE", meta["guide_title"]).replace("GUIDE_URL", meta["guide"])

        content = (TEMPLATE
            .replace("TITLE", meta["title"])
            .replace("DESCRIPTION", meta["description"])
            .replace("SLUG", slug)
            .replace("GUIDE_SECTION", guide_section)
        )

        index_path = os.path.join(cat_dir, "index.md")
        with open(index_path, "w") as f:
            f.write(content)
        print(f"Created {index_path}")

    print(f"\nGenerated {len(CATEGORIES)} category index pages.")

if __name__ == "__main__":
    main()
