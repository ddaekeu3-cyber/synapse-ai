---
layout: solution
title: "Password auth mode rejects devices that send credentials via connectAuth.token (e.g. Rabbit R1)"
category: auth
source: https://github.com/openclaw/openclaw/issues/51953
---

# Password auth mode rejects devices that send credentials via connectAuth.token (e.g. Rabbit R1)

## 증상
When the gateway is configured with `gateway.auth.mode = "password"` (required by Tailscale Funnel), devices that send their credentials via `connectAuth.token` instead of `connectAuth.password` are rejected with `reason=password_missing`, even when the correct password is in the token field.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Manually patching the bundled dist files (`auth-profiles-*.js` and `reply-*.js`) with the above change. This breaks on every update.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51953
