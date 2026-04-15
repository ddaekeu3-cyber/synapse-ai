---
layout: solution
title: "Gateway crashes when receiving group messages — auto-writes invalid 'agents.subagents' config field (v2026.3.2)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39468
description: "OpenClaw v2026.3.2 has a critical bug: when the gateway receives group messages from Feishu (Lark), it automatically writes an invalid field to , causing"
---

# Gateway crashes when receiving group messages — auto-writes invalid 'agents.subagents' config field (v2026.3.2)

## 증상
OpenClaw v2026.3.2 has a critical bug: when the gateway receives group messages from Feishu (Lark), it automatically writes an invalid `agents.subagents` field to `openclaw.json`, causing config validation to fail and the gateway to crash.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
1. Set config file to read-only: `chmod 444 ~/.openclaw/openclaw.json`
2. Remove `agents.list` and use global `messages.groupChat.mentionPatterns` instead
3. Disable all but one Feishu account to avoid conflicts

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39468
