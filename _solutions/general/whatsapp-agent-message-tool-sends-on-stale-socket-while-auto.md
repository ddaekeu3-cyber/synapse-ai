---
layout: solution
title: "[WhatsApp] Agent message tool sends on stale socket while auto-reply uses live socket"
category: general
source: https://github.com/openclaw/openclaw/issues/47154
description: "When the agent's tool sends a WhatsApp message, it fails with \"No active WhatsApp Web listener\" while the auto-reply path succeeds at the exact same time."
---

# [WhatsApp] Agent message tool sends on stale socket while auto-reply uses live socket

## 증상
When the agent's `message` tool sends a WhatsApp message, it fails with "No active WhatsApp Web listener" while the auto-reply path succeeds at the **exact same time**. The health-monitor detects stale sockets and restarts, but the `message` tool's send path still holds a reference to the dead socket.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
The `message` tool should retry with a longer backoff when it gets a "No active WhatsApp Web listener" error, waiting for the socket to reconnect (typically 2-5 seconds). Currently it gives up immediately.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47154
