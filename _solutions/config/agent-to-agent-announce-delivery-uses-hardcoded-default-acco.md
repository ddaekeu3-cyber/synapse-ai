---
layout: solution
title: "Agent-to-agent announce delivery uses hardcoded 'default' accountId instead of session's lastAccountId"
category: config
source: https://github.com/openclaw/openclaw/issues/51626
---

# Agent-to-agent announce delivery uses hardcoded 'default' accountId instead of session's lastAccountId

## 증상
When using `sessions_send` for agent-to-agent communication in a multi-agent Discord setup, the **announce step** (which posts the target agent's reply back to their channel) always resolves to `accountId: "default"` instead of using the session's `lastAccountId`.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Adding `accounts.default` with a valid bot token (we used the main bot's token) allows delivery to succeed, but the message appears as the wrong bot identity.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51626
