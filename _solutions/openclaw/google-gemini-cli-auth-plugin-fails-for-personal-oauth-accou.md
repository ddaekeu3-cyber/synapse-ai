---
layout: solution
title: "google-gemini-cli-auth plugin fails for personal OAuth accounts"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49226
---

# google-gemini-cli-auth plugin fails for personal OAuth accounts

## 증상
**Version**: OpenClaw 2026.3.13

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently, users can only use Google models through:
1. API key path (`google/gemini-*`) - limited by free tier quota (20 req/day)
2. Not using Google models through OpenClaw at all

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49226
