---
layout: solution
title: "OOM crash: loadCombinedSessionStoreForGateway loads all agent session stores simultaneously"
category: openclaw
---

# OOM crash: loadCombinedSessionStoreForGateway loads all agent session stores simultaneously

## 증상
OpenClaw's gateway crashes with an out-of-memory (OOM) error when `loadCombinedSessionStoreForGateway` is called during hook dispatch. The function eagerly loads **every agent's full `sessions.json`** into memory simultaneously — even when only a single agent's session store is needed. Under product

에러 메시지:
```
FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #51264 참조.

## 해결법
es

In priority order:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51264
