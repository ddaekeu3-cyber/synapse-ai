---
layout: solution
title: "voice-call: outbound conversation mode race — initial message falls back to Polly before media stream connects"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/36869
description: "Outbound calls in mode never use ElevenLabs TTS for the initial message. The message is spoken via the TwiML (Polly) fallback and then consumed, leaving"
---

# voice-call: outbound conversation mode race — initial message falls back to Polly before media stream connects

## 증상
Outbound calls in `conversation` mode never use ElevenLabs TTS for the initial message. The message is spoken via the TwiML `<Say>` (Polly) fallback and then consumed, leaving the WebSocket media stream with nothing to say when it connects. **Inbound calls work correctly with ElevenLabs.**

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Use `--mode notify` for outbound calls (one-way, uses inline TwiML). Conversation mode is broken for the initial message on outbound.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/36869
