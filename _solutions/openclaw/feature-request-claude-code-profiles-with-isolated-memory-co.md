---
layout: solution
title: "Feature request: Claude Code profiles with isolated memory, commands, hooks, and settings"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/7075
description: "The Claude Code ecosystem has grown at breakneck speed. There are now dozens of batteries-included frameworks that enhance Claude with powerful context"
---

# Feature request: Claude Code profiles with isolated memory, commands, hooks, and settings

## 증상
The Claude Code ecosystem has grown at breakneck speed. There are now dozens of batteries-included frameworks that enhance Claude with powerful context and subagent libraries, e.g.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Introduce a new profiling feature into Claude Code that lets users configure a specific profile.

A new `profiles` directory would be added to `.claude`.

```tree
.claude
└── profiles
    ├── claudekit
    │   ├── settings.json
    │   ├── commands/
    │   ├── context/
    │   └── hooks/
    └── superclaude
        ├── settings.json
        ├── agents/
        └── commands/
```

Then, users would do something like this:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/7075
