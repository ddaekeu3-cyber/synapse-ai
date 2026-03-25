---
layout: solution
title: "Bug: openai-codex OAuth token shows 'expires in 0m' despite valid JWT (display issue)"
category: auth
source: https://github.com/openclaw/openclaw/issues/53723
---

# Bug: openai-codex OAuth token shows 'expires in 0m' despite valid JWT (display issue)

## 증상
`openclaw models status` consistently reports `openai-codex:default ok expires in 0m` even when the underlying JWT access token has a valid expiry well into the future.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
None for the display. The token actually works fine — this is a cosmetic/UX issue but causes confusion and unnecessary re-auth attempts.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53723
