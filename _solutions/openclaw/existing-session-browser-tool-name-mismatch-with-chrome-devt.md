---
layout: solution
title: "existing-session browser: tool name mismatch with chrome-devtools-mcp + timeout"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48182
---

# existing-session browser: tool name mismatch with chrome-devtools-mcp + timeout

## 증상
The `existing-session` browser profile (`driver: "existing-session"`) fails to work with `chrome-devtools-mcp` due to two issues:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None for existing-session. Managed CDP profiles still work but don't have access to user's logged-in session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48182
