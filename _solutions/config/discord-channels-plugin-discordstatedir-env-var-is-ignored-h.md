---
layout: solution
title: "Discord Channels plugin: DISCORD_STATE_DIR env var is ignored (hardcoded path prevents multi-bot setup)"
category: config
source: https://github.com/anthropics/claude-code/issues/38312
---

# Discord Channels plugin: DISCORD_STATE_DIR env var is ignored (hardcoded path prevents multi-bot setup)

## 증상
The Discord channel plugin (`plugin:discord@claude-plugins-official`) hardcodes the state directory path in `server.ts`:

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
ineffective.

## Expected behavior

Each Claude Code session should be able to use its own Discord bot with a separate state directory:

```bash
export DISCORD_STATE_DIR=~/.claude/channels/discord-bot1
export DISCORD_BOT_TOKEN=<token1>
claude --channels plugin:discord@claude-plugins-official

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38312
