---
layout: solution
title: "Telegram plugin MCP server disconnects despite active polling"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/36427
description: "The official Telegram plugin () MCP server repeatedly disconnects during a session. The bot can send messages via tools, but inbound messages from"
---

# Telegram plugin MCP server disconnects despite active polling

## 증상
The official Telegram plugin (`claude-plugins-official/telegram/0.0.1`) MCP server repeatedly disconnects during a session. The bot can **send** messages via tools, but **inbound** messages from Telegram never arrive because the plugin process gets killed/disconnected.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually polling via `curl .../getUpdates` and sending via `curl .../sendMessage` works — the bot API is fully functional, only the MCP bridge is broken.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36427
