---
layout: solution
title: "Subagent completion signal not received by parent session, message queue stuck    子Agent完成后主会话收不到完成信号，消息队列卡住"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50747
---

# Subagent completion signal not received by parent session, message queue stuck    子Agent完成后主会话收不到完成信号，消息队列卡住

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Use `mode: "run"` instead of `mode: "session"`
- Avoid `sessions_yield` + `streamTo: "parent"` combination
- Set `runTimeoutSeconds` parameter
- For document tasks, consider executing directly in parent session

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50747
