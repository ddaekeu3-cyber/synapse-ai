---
layout: solution
title: "Relay permission prompts to Discord channel for headless setups"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37797
---

# Relay permission prompts to Discord channel for headless setups

## 증상
When running Claude Code headlessly with `--channels plugin:discord@claude-plugins-official`, permission prompts block the session until someone physically accesses the terminal. There's no way to know the bot is stuck unless you check the tmux session.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- SSH into the server and attach to the tmux session to manually approve prompts
- Pre-configure settings to minimize prompts

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37797
