---
layout: solution
title: "Memory flush skipped on timeout in runEmbeddedPiAgent"
category: openclaw
---

# Memory flush skipped on timeout in runEmbeddedPiAgent

## 증상
When `runEmbeddedPiAgent` times out during a memory flush (`trigger === "memory"`), the code returns early with an error, skipping the logic that saves `systemPromptReport` to `memory/YYYY-MM-DD.md`.

에러 메시지:
```javascript
// Current code (lines 18097-18115):
if (timedOut && !timedOutDuringCompaction && payloads.length === 0) return {
    payloads: [{
        text: "Request timed out before a response was 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #44241 참조.

## 해결법
Modify the timeout handling logic to check `if (params.trigger === "memory" && attempt.systemPromptReport)` before returning the error. If true, return success (`isError: false`) to allow the memory save logic to complete, even if the request timed out.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/44241
