---
layout: solution
title: "Discord: asVoice conflicts with required components schema — cannot send voice messages with pre-recorded audio"
category: general
source: https://github.com/openclaw/openclaw/issues/51447
---

# Discord: asVoice conflicts with required components schema — cannot send voice messages with pre-recorded audio

## 증상
The `message` tool requires `components` for Discord sends, but Discord voice messages (`asVoice: true`) cannot have components. This makes it **impossible to send pre-recorded audio as inline playable voice messages in Discord**.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
The `tts` tool can send inline playable audio, but it uses the bot's own TTS voice — not custom/cloned audio. There is no way to send pre-recorded custom audio as a playable voice message through the current tool.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51447
