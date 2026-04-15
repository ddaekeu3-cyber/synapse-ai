---
layout: solution
title: "Feature: Per-channel model overrides in Discord guild config"
category: config
source: https://github.com/openclaw/openclaw/issues/32301
description: "Currently only supports . Would love per-channel model"
---

# Feature: Per-channel model overrides in Discord guild config

## 증상
Currently `channels.discord.guilds.<guildId>` only supports `requireMention`. Would love per-channel model routing.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
today is bootstrapping sessions with `/model` commands or behavioral routing rules in `AGENTS.md`, but this isn't reliable on the first message of a new session since the session starts on the default model before any routing logic runs.

Native config-level routing would make multi-tier Discord setups much cleaner. A category-level override would also be useful.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32301
