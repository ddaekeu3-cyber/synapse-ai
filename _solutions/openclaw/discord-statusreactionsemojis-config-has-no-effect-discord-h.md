---
layout: solution
title: "Discord: statusReactions.emojis config has no effect — Discord handler omits emojis param when creating controller"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/23145
---

# Discord: statusReactions.emojis config has no effect — Discord handler omits emojis param when creating controller

## 증상
The `messages.statusReactions.emojis` config key is silently ignored on Discord. The feature itself works (reactions fire), but custom emoji overrides have no effect — `DEFAULT_EMOJIS` are always used.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None. The default emojis (🤔, 👨‍💻, ⚡, 🔥, 👍, 😱, 🥱, 😨) are always used on Discord regardless of config.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/23145
