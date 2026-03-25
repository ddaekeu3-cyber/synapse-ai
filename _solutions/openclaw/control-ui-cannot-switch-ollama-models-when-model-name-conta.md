---
layout: solution
title: "Control UI cannot switch Ollama models when model name contains slashes"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50509
---

# Control UI cannot switch Ollama models when model name contains slashes

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
the issue.
```

### Impact and severity

Affected users: Users with Ollama models whose names contain slashes
Severity: Blocks workflow — cannot switch models via Control UI
Frequency: Always (reproducible every time)
Consequence: Users must use /model CLI command to switch, which is inconvenient

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50509
