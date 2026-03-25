---
layout: solution
title: "Exec/ACP completion wake doesn't trigger turns in group chat sessions (Discord/Telegram channels)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47578
---

# Exec/ACP completion wake doesn't trigger turns in group chat sessions (Discord/Telegram channels)

## 증상
Background exec completion and ACP session completion do not trigger agent turns in group chat sessions (Discord channels, Telegram groups). The system event is enqueued and `requestHeartbeatNow` is called with the correct `sessionKey`, but the heartbeat response is never delivered to the group chat session.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
We use a separate Discord bot ("Claude Code Notifier") that sends an `@mention` to the channel when Claude Code finishes. OpenClaw treats this as a normal inbound message and triggers a turn. This bypasses the heartbeat/system-event pipeline entirely.

Details: The Claude Code Stop hook (or chained script after `claude --print`) curls the Discord API via a separate bot with a different `application_id`. Config requires `allowBots: "mentions"` and the bot in the guild user allowlist.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47578
