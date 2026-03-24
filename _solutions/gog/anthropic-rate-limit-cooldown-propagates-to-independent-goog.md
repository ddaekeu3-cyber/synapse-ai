---
layout: solution
title: "Anthropic rate limit cooldown propagates to independent google-vertex fallback provider"
category: gog
---

# Anthropic rate limit cooldown propagates to independent google-vertex fallback provider

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```
17:26:01 [model-fallback/decision] candidate_failed candidate=anthropic/claude-sonnet-4-6 reason=rate_limit next=google-vertex/gemini-2.5-flash
17:26:10 [agent/embedded] isError=true model=gemini-

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53233 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53233
