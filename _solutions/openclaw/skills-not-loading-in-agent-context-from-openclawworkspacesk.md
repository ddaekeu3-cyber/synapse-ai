---
layout: solution
title: "Skills not loading in agent context from ~/.openclaw/workspace/skills/"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43735
description: "The OpenClaw agent context is not loading all available skills from directory. Only a subset of skills appear in the initial context under , causing"
---

# Skills not loading in agent context from ~/.openclaw/workspace/skills/

## 증상
The OpenClaw agent context is not loading all available skills from `~/.openclaw/workspace/skills/` directory. Only a subset of skills appear in the initial context under `<available_skills>`, causing agents to be unaware of existing skills.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually scan the skills directory when needed:
```bash
ls ~/.openclaw/workspace/skills/
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43735
