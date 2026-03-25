---
layout: solution
title: "Claude Desktop: Local Claude Code sessions crash immediately with exit code 1 (429 on OAuth endpoint)"
category: auth
source: https://github.com/anthropics/claude-code/issues/36653
---

# Claude Desktop: Local Claude Code sessions crash immediately with exit code 1 (429 on OAuth endpoint)

## 증상
- **Claude Desktop**: v1.1.7714 (macOS, darwin, arm64)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
- Wait 15-30 minutes for the rate limit to expire before retrying
- Avoid rapidly restarting the app, as each attempt worsens the rate limit
- Use the standalone Claude Code CLI which is unaffected

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36653
