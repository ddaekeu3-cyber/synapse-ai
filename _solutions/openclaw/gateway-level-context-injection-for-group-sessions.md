---
layout: solution
title: "Gateway-level context injection for group sessions"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/34437
---

# Gateway-level context injection for group sessions

## 증상
Add the ability to inject context files into agent sessions based on the group/channel the message originates from, configured at the gateway level rather than relying on agent-side procedural instructions.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
PROCEDURES.md §11 instructs the agent to read context files based on a routing table. Works when the agent follows it, but unverifiable without manual spot-checks.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34437
