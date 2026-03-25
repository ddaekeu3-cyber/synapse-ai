---
layout: solution
title: "[Feature]: Re-inject Telegram DM session history after context compaction"
category: telegram
source: https://github.com/openclaw/openclaw/issues/28911
---

# [Feature]: Re-inject Telegram DM session history after context compaction

## 증상
After context compaction fires on a Telegram DM session, the recent conversation history is completely lost. The agent starts fresh with no knowledge of what was just being discussed. The user has no warning this happened — from their side, the conversation just broke.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
is to manually call `sessions_history` after detecting context loss, but this requires the agent to know to do it and is not seamless.
- This affects every user running long DM conversations — compaction is automatic and users don't control when it fires.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/28911
