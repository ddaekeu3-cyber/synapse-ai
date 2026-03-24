---
layout: solution
title: "Unhandled Playwright assertion error in CRSession._onMessage crashes Gateway"
category: openclaw
---

# Unhandled Playwright assertion error in CRSession._onMessage crashes Gateway

## 증상
An unhandled `Assertion error` from Playwright's Chrome DevTools Protocol (CDP) session management crashes the entire Gateway process. The error is not caught by OpenClaw, causing a full process exit that requires launchd to restart.

에러 메시지:
```
[ERROR] Unhandled promise rejection: Error: Assertion error
    at assert (openclaw/node_modules/playwright-core/lib/utils/isomorphic/assert.js:26:11)
    at CRSession._onMessage (openclaw/node_mo

## 원인
원본 이슈에서 확인 필요. GitHub Issue #45224 참조.

## 해결법
Wrap the CDP message handling in `CRSession._onMessage` / `CRConnection._onMessage` with a try-catch, or add a global `unhandledRejection` handler in the Gateway that logs and recovers rather than crashing. The browser subsystem's errors should be isolated and not take down the entire Gateway process.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/45224
