---
layout: solution
title: "macOS: StreamingAudioPlayback crash during ElevenLabs Talk Mode playback on Intel x86_64"
category: performance
source: https://github.com/openclaw/openclaw/issues/51886
---

# macOS: StreamingAudioPlayback crash during ElevenLabs Talk Mode playback on Intel x86_64

## 증상
Crash (process/app exits or hangs)

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Remove API key from `talk.providers.elevenlabs.apiKey` to fall back to system voice for Talk Mode. Text chat TTS via `messages.tts` continues to work with ElevenLabs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51886
