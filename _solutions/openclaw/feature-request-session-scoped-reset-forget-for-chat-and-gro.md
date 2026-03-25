---
layout: solution
title: "Feature request: session-scoped reset / forget for chat and group contexts"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50790
---

# Feature request: session-scoped reset / forget for chat and group contexts

## 증상
OpenClaw currently appears to lack a first-class way to reset or forget a specific session context without affecting the broader runtime.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Today, the most targeted workaround seems to be manual deletion/removal of the specific session file and related index entry.

Why people may do this:

- it is narrower than restarting the gateway
- it avoids disturbing unrelated sessions
- it more closely matches the real target of the operation: the session record itself

But this has several problems:

- it is not an official supported workflow
- it requires storage-level knowledge
- it is easy to remove the wrong thing
- it is not auditable from product UX
- it is hard to expose safely to non-technical operators

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50790
