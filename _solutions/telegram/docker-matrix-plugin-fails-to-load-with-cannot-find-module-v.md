---
layout: solution
title: "Docker: Matrix plugin fails to load with 'Cannot find module '@vector -im/matrix-bot-sdk'' (Require stack: /app/dist/matrix-*.js) — persistent even after cleanup/rebuild"
category: telegram
source: https://github.com/openclaw/openclaw/issues/50039
description: "Regression (worked before, now"
---

# Docker: Matrix plugin fails to load with "Cannot find module '@vector -im/matrix-bot-sdk'" (Require stack: /app/dist/matrix-*.js) — persistent even after cleanup/rebuild

## 증상
Regression (worked before, now fails)

## 원인
GitHub Issue #50039에서 보고된 버그. 해결법이 이슈에 포함됨.

## 해결법
- `--no-cache` rebuilds + `--force-recreate`

None resolved the issue.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50039
