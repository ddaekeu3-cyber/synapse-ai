---
layout: solution
title: "Edge TTS generates audio but sendVoice fails for model-reply TTS tags in Telegram (auto / tagged mode)"
category: hallucination
source: https://github.com/openclaw/openclaw/issues/45329
---

# Edge TTS generates audio but sendVoice fails for model-reply TTS tags in Telegram (auto / tagged mode)

## 증상
Behavior bug (incorrect output/state without crash)

## 원인
보고된 버그/문제. 카테고리: hallucination.

## 해결법
### Additional information

• The /tts audio <text> slash command works correctly — Telegram bot credentials and sendVoice API are functional
• Both auto: "always" and auto: "tagged" fail; issue is not mode-specific
• Including [[tts]] as first token in model reply (tagged mode) also fails
• Workaround: generate MP3 manually with node-edge-tts and reply with   + MEDIA:./voice.mp3 on separate lines

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45329
