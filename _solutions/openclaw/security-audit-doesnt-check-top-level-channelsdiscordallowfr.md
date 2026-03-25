---
layout: solution
title: "Security audit doesn't check top-level channels.discord.allowFrom for slash commands"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39341
---

# Security audit doesn't check top-level channels.discord.allowFrom for slash commands

## 증상
Security audit reports "Discord slash commands have no allowlists" warning even when `channels.discord.allowFrom` is configured with a valid user ID.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
should include `discordCfg.allowFrom` in the `ownerAllowFromConfigured` check, similar to how it's done for the Telegram channel.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39341
