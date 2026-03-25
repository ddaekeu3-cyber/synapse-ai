---
layout: solution
title: "[Feature]: Add Discord to VOICE_BUBBLE_CHANNELS for inline voice messages"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/44633
---

# [Feature]: Add Discord to VOICE_BUBBLE_CHANNELS for inline voice messages

## 증상
TTS audio sent to Discord text channels should be delivered as inline voice messages (voice bubbles with waveform) rather than file attachments.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
is patching the dist bundle to add "discord" to the Set, which gets overwritten on every update.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/44633
