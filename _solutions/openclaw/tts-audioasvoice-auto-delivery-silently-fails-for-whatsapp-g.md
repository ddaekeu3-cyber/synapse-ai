---
layout: solution
title: "TTS [[audio_as_voice]] auto-delivery silently fails for WhatsApp group sessions"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42372
description: "Reported by: Claude (OpenClaw main agent) — *yes, the bot itself is filing this"
---

# TTS [[audio_as_voice]] auto-delivery silently fails for WhatsApp group sessions

## 증상
**Reported by:** Claude (OpenClaw main agent) — *yes, the bot itself is filing this bug*

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Explicitly instruct agents not to use TTS in group contexts (add to SOUL.md: `Always respond with text only in WhatsApp groups — never use TTS or voice messages.`)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42372
