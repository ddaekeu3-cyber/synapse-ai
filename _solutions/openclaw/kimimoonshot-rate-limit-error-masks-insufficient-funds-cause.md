---
layout: solution
title: "Kimi/Moonshot 'Rate Limit' error masks insufficient funds, causes UI lockout"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43447
---

# Kimi/Moonshot 'Rate Limit' error masks insufficient funds, causes UI lockout

## 증상
- **OpenClaw Version:** 2026.3.8

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Reload funds into Kimi account, or force model switch via control UI dropdown (bypasses chat parser).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43447
