---
layout: solution
title: "RFC: Self-Healing Tool Router — Deterministic Fallback Routing for Tool Failures"
category: openclaw
---

# RFC: Self-Healing Tool Router — Deterministic Fallback Routing for Tool Failures

## 증상
Add a deterministic fallback routing layer to OpenClaw's tool execution pipeline. When a tool fails mid-execution, the router checks a fallback table before returning the error to the LLM. Predictable failures (timeouts, network errors, permission issues) get deterministic recovery. The LLM is only 

에러 메시지:
```yaml
tools:
  fallbackRouting:
    enabled: true
    maxRetries: 2            # per-tool-call retry cap
    healthWindow: 300        # seconds to track failure rate
    routes:
      - tool: web_fe

## 원인
원본 이슈에서 확인 필요. GitHub Issue #33809 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/33809
