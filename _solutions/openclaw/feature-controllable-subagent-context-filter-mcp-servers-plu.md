---
layout: solution
title: "[FEATURE] Controllable subagent context: filter MCP servers, plugins, skills, and rules per agent"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38044
description: "When users have many MCP servers, plugins, skills, and rules configured, subagents fail immediately with \"prompt is too long\" because they inherit the"
---

# [FEATURE] Controllable subagent context: filter MCP servers, plugins, skills, and rules per agent

## 증상
When users have many MCP servers, plugins, skills, and rules configured, **subagents fail immediately with "prompt is too long"** because they inherit the full system prompt including all MCP tool schemas. This makes Explore, Plan, and general-purpose agents completely unusable for power users.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
it.
- **#31623** proposes extending deferred tool loading to subagents. This issue proposes a broader solution with explicit context control.
- **#23448** documents mass agent failures. Related but focused on a different scenario.

All three issues describe symptoms of the same missing capability: **subagent context is not controllable**.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38044
