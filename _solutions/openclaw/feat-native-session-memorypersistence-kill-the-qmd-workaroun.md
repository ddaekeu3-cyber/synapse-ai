---
layout: solution
title: "feat: native session memory/persistence — kill the QMD workaround dependency"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39885
---

# feat: native session memory/persistence — kill the QMD workaround dependency

## 증상
"It forgets context between sessions" is the second most-cited complaint about OpenClaw. The current workaround (MEMORY.md + community QMD patterns) is functional but fragile, undiscoverable, and being used by Anthropic's free Claude memory pitch to poach users. This issue proposes shipping first-class session persistence as a native OpenClaw feature.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. Maintain a `MEMORY.md` file in the workspace
2. Inject it into context via system prompt or heartbeat
3. Use cron jobs to consolidate and update it nightly

This works, but:
- Requires significant manual setup
- Is not documented as an official pattern
- Breaks or degrades as MEMORY.md grows beyond ~50KB
- Is invisible to new users who don't know the community pattern exists
- Anthropic's Claude explicitly markets "Claude remembers you" as a differentiator — directly addressing this gap

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39885
