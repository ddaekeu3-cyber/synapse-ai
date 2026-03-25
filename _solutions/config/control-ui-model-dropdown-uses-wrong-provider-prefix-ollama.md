---
layout: solution
title: "Control UI model dropdown uses wrong provider prefix (ollama/ for all models)"
category: config
source: https://github.com/openclaw/openclaw/issues/46577
---

# Control UI model dropdown uses wrong provider prefix (ollama/ for all models)

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
GitHub Issue #46577에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
Using the text input instead of dropdown works:
- Type `sonnet` or `anthropic/claude-sonnet-4-6` manually
- Use `/model sonnet` command

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46577
