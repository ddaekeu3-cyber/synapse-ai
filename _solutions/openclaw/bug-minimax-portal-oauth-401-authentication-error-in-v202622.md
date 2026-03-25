---
layout: solution
title: "Bug: minimax-portal OAuth 401 authentication error in v2026.2.26"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/28386
---

# Bug: minimax-portal OAuth 401 authentication error in v2026.2.26

## 증상
minimax-portal OAuth authentication fails with HTTP 401 error in openclaw v2026.2.26, but works correctly in v2026.2.24.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Downgrade to v2026.2.24:
```bash
npm install -g openclaw@2026.2.24
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28386
