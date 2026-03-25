---
layout: solution
title: "voice-call: add postCall hook to notify originating session with transcript summary"
category: loop-stuck
source: https://github.com/openclaw/openclaw/issues/29164
---

# voice-call: add postCall hook to notify originating session with transcript summary

## 증상
When an agent initiates a voice call via `voice_call(action=initiate_call)`, the call runs autonomously in its own session (`voice:{phone}`). When the call ends, the transcript is persisted to `calls.jsonl`, but **nothing notifies the originating agent session** that the call completed.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Using heartbeat checks to periodically scan `calls.jsonl` for completed calls and send summaries. Works but adds delay (up to 1 heartbeat interval).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29164
