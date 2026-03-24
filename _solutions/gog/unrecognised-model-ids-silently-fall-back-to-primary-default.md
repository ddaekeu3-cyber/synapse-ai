---
layout: solution
title: "Unrecognised model IDs silently fall back to primary default — bypasses configured fallback chain and tool permissions"
category: gog
---

# Unrecognised model IDs silently fall back to primary default — bypasses configured fallback chain and tool permissions

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
Test Matrix — 12 Models Tested (2026-03-06)

*xAI Models:*

`**grok-4-1-fast**`
• Dots?: No
• Result: ✅ Works

`**grok-4-1-fast-reasoning**`
• Dots?: No
• Result: ❌ Fallback (model not found)

## 원인
원본 이슈에서 확인 필요. GitHub Issue #37813 참조.

## 해결법
successfully — both appear to be on an internal whitelist: `grok-4-1-fast` and `gemini-3-flash-preview`. All others fail, including model IDs with no dots/periods (ruling out a parsing issue with dots in version strings).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/37813
