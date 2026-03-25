---
layout: solution
title: "Feature Request: Real-time sub-agent conversation forwarding to parent channel"
category: telegram
source: https://github.com/openclaw/openclaw/issues/27029
---

# Feature Request: Real-time sub-agent conversation forwarding to parent channel

## 증상
When using `sessions_spawn` to create sub-agents, it would be helpful to have an option to forward the sub-agent's conversation in real-time to the parent session's channel (e.g., Telegram, Discord).

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
- Manually checking `sessions_history` periodically
- Using `tail -f` on session JSONL files

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/27029
