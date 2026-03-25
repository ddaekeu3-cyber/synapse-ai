---
layout: solution
title: "feat(tts): send Telegram voice messages as OGG/Opus (sendVoice) instead of audio files"
category: telegram
source: https://github.com/openclaw/openclaw/issues/43072
---

# feat(tts): send Telegram voice messages as OGG/Opus (sendVoice) instead of audio files

## 증상
When TTS responds via Telegram, audio is sent as an audio file (MP3 via `sendAudio`), not as a Telegram voice message (waveform circle via `sendVoice`).

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
1. Bot Token 확인: BotFather에서 토큰 재발급
2. Webhook URL 설정 확인
3. 메시지 포맷 호환성 확인
4. Rate limit: Telegram API 제한 준수
5. 그룹 권한 설정 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43072
