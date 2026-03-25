---
layout: solution
title: "Rate Limit Message Infinite Loop"
category: rate-limit
source: https://github.com/anthropics/claude-code/issues/18388
---

# Rate Limit Message Infinite Loop

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
Currently, the only solution is:
Force kill Claude Code process (kill -9 or Activity Monitor)
Wait for rate limit reset time
Restart Claude Code 
System Info
macOS Darwin 25.2.0
Session timezone: Europe/Dublin (UTC+0)
Occurred: 2026-01-15 around 8:00-10:00 AM
Session type: Long-running (10+ hours active before issue)

<img width="741" height="855" alt="Image" src="https://github.com/user-attachments/assets/86544242-978d-4a44-8268-33a7c909db8c" />

<img width="708" height="990" alt="Image" src="https://github.com/user-attachments/assets/0192e25c-07c5-47ee-aeb7-b93d130df25c" />

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/18388
