---
layout: solution
title: "Anthropic API key fallback not triggered when subscription hits rate limit — both providers share cooldown bucket"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/30030
---

# Anthropic API key fallback not triggered when subscription hits rate limit — both providers share cooldown bucket

## 증상
**Version:** OpenClaw 2026.2.26 (bc50708)

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
None currently. Both providers fail simultaneously, leaving the agent completely unavailable until the cooldown expires.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/30030
