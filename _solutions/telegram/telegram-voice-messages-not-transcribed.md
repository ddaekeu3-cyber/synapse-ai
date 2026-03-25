---
layout: solution
title: "Telegram Voice Messages Not Transcribed"
category: telegram
source: https://github.com/openclaw/openclaw/issues/17101
---

# Telegram Voice Messages Not Transcribed

## 증상
Telegram voice messages (`.ogg` with Opus codec) are received by OpenClaw but are not automatically transcribed. The agent receives them as raw audio file attachments (`<media:audio>`) instead of transcribed text, despite `tools.media.audio.enabled` being set to `true` and valid transcription models configured.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
None currently available. Users must type text messages instead of using voice.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/17101
