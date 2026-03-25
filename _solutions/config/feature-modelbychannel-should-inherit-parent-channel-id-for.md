---
layout: solution
title: "[Feature]: modelByChannel should inherit parent channel ID for Discord threads"
category: config
source: https://github.com/openclaw/openclaw/issues/30760
---

# [Feature]: modelByChannel should inherit parent channel ID for Discord threads

## 증상
When a Discord channel is configured in `channels.modelByChannel`, the override only applies to messages sent directly in that channel. Discord threads spawn as separate sessions with their own IDs, and `modelByChannel` performs an exact key match — so the parent channel override never applies to thread sessions.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
should use it as a fallback key during modelByChannel resolution.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30760
