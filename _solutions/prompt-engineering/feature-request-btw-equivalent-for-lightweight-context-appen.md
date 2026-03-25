---
layout: solution
title: "Feature Request: /btw equivalent for lightweight context append"
category: prompt-engineering
source: https://github.com/openclaw/openclaw/issues/45122
---

# Feature Request: /btw equivalent for lightweight context append

## 증상
When using OpenClaw via messaging surfaces (Discord, Telegram, etc.), every message triggers a full turn with complete context (system prompt, workspace files, history). This uses significant tokens even for small additions like "oh, and also do X".

## 원인
보고된 버그/문제. 카테고리: prompt-engineering.

## 해결법
Users must send a full message, which triggers complete context reload. No lightweight append option exists.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45122
