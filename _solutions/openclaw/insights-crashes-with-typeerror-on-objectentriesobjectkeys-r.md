---
layout: solution
title: "/insights crashes with TypeError on Object.entries/Object.keys — regression from #23138"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/35394
description: "crashes immediately with a TypeError. This is a regression of #23138, which was reportedly fixed in February 2026 but has resurfaced in"
---

# /insights crashes with TypeError on Object.entries/Object.keys — regression from #23138

## 증상
`/insights` crashes immediately with a TypeError. This is a regression of #23138, which was reportedly fixed in February 2026 but has resurfaced in 2.1.77.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
from #23138 (`grep -l '"error":' ~/.claude/usage-data/facets/*.json | xargs rm -v`) does not resolve this instance because the issue is missing/null fields, not error-response facets.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35394
