---
layout: solution
title: "Fix: Fallback mechanism never triggers due to per-model timeout equaling global run timeout"
category: openclaw
---

# Fix: Fallback mechanism never triggers due to per-model timeout equaling global run timeout

## 증상
In the current implementation of OpenClaw, the model fallback mechanism fails to trigger when an LLM provider hangs. The agent instead waits for 60 seconds and then completely aborts the run with an `embedded run timeout` error.

에러 메시지:
` error.

## Root Cause
In `

## 원인
원본 이슈에서 확인 필요. GitHub Issue #43400 참조.

## 해결법
per-model time budget === */`), the per-model timeout is calculated as `Math.max(MIN_PER_MODEL_MS, Math.floor(MIN_PER_MODEL_MS * 4))` where `MIN_PER_MODEL_MS = 15000`. This results in a per-model timeout of exactly 60 seconds.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/43400
