---
layout: solution
title: "Discord: allow editing components v2 messages without text message field"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/23605
---

# Discord: allow editing components v2 messages without text message field

## 증상
The `message` tool's `edit` action requires the `message` field (text content), but Discord components v2 messages have empty text content by design. This means components v2 messages cannot be edited in-place — the edit is rejected by OpenClaw validation before reaching the Discord API.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
is delete → resend → re-pin, which creates noisy pin system messages and loses message continuity.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/23605
