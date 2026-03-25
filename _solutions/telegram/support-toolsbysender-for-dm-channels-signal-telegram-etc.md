---
layout: solution
title: "Support toolsBySender for DM channels (Signal, Telegram, etc.)"
category: telegram
source: https://github.com/openclaw/openclaw/issues/53760
---

# Support toolsBySender for DM channels (Signal, Telegram, etc.)

## 증상
`toolsBySender` currently works for group/channel contexts (IRC, Slack, MS Teams, Discord) but is not available for DM-level policies on channels like Signal and Telegram.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Prose-level constraints in workspace files (e.g., AGENTS.md) instructing the agent not to use tools when the sender is a peer. This works but is not enforced at the platform level.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53760
