---
layout: solution
title: "Bug: Cache Usage Exceeds 100% Causing Rate Limit Errors"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/27423
---

# Bug: Cache Usage Exceeds 100% Causing Rate Limit Errors

## 증상
When using OpenClaw for extended sessions with many tool calls, the cache usage percentage can exceed 100% (observed 872%-930%), triggering rate limit errors that block all operations.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
`powershell
Remove-Item C:\Users\lengx\.openclaw\cache\* -Recurse -Force
openclaw gateway restart
`

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27423
