---
layout: solution
title: "feat: Gateway response timeout watchdog for stale sessions"
category: general
---

# feat: Gateway response timeout watchdog for stale sessions

## 증상
When a session receives a user message but fails to respond (due to hanging, context overflow, or silent failure), there is no mechanism to detect this. The gateway currently uses fire-and-forget delivery — once a message is dispatched to a session, there is no tracking of whether a response was pro



## 원인
원본 이슈에서 확인 필요. GitHub Issue #43851 참조.

## 해결법
Add a **response timeout watchdog** at the gateway level:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/43851
