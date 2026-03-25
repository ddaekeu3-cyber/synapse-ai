---
layout: solution
title: "session-memory hook silently fails to write files (v2026.3.12 and v2026.3.13)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45819
---

# session-memory hook silently fails to write files (v2026.3.12 and v2026.3.13)

## 증상
**Version:** 2026.3.12 (confirmed), 2026.3.13 (still present after update)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manually writing memory files from inside the session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45819
