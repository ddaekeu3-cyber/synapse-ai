---
layout: solution
title: "`devices list` / `nodes status` timeout while `gateway status` shows `RPC probe: ok` (regression in 2026.3.12/2026.3.13)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46316
---

# `devices list` / `nodes status` timeout while `gateway status` shows `RPC probe: ok` (regression in 2026.3.12/2026.3.13)

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Roll back to 2026.3.8

Hypothesis:
- WebSocket handshake/connection handling regression where lightweight probe path succeeds but full command RPC path intermittently times out under runtime load.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46316
