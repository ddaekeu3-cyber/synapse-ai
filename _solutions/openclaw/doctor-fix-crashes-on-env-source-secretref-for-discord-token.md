---
layout: solution
title: "doctor --fix crashes on env-source SecretRef for Discord token"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50537
---

# doctor --fix crashes on env-source SecretRef for Discord token

## 증상
`openclaw doctor --fix` crashes with an unresolved SecretRef error when `channels.discord.accounts.default.token` is configured as an env-source SecretRef. The gateway resolves the token correctly at runtime (Discord is fully functional), but doctor fails before completing checks.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
for those issues apparently did not cover the env-source path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50537
