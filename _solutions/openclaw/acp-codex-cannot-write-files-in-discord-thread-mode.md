---
layout: solution
title: "ACP (Codex) cannot write files in Discord thread mode"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/28484
---

# ACP (Codex) cannot write files in Discord thread mode

## 증상
- ACP enabled with `spawnAcpSessions: true`

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None currently. User must use main agent (MiniMax) for file operations.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28484
