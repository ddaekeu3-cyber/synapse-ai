---
layout: solution
title: "TypeError: A.with is not a function — regression in 2.1.78 on Node.js 18"
category: docker
source: https://github.com/anthropics/claude-code/issues/35934
description: "Claude Code 2.1.78 introduced a that kills sessions mid-execution. Not present in 2.1.74-2.1.77. Discovered while running Terminal-Bench (89 coding tasks)"
---

# TypeError: A.with is not a function — regression in 2.1.78 on Node.js 18

## 증상
Claude Code 2.1.78 introduced a `TypeError: A.with is not a function` that kills sessions mid-execution. Not present in 2.1.74-2.1.77. Discovered while running Terminal-Bench (89 coding tasks) on Modal containers.

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
Pin to 2.1.77 or earlier:
```bash
npm install -g @anthropic-ai/claude-code@2.1.77
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35934
