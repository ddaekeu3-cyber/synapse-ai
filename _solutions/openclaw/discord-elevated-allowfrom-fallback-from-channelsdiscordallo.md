---
layout: solution
title: "Discord elevated allowFrom fallback from channels.discord.allowFrom does not work on 2026.3.22"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53198
---

# Discord elevated allowFrom fallback from channels.discord.allowFrom does not work on 2026.3.22

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
enabling elevated alone; it started working only after duplicating the allowlist under `tools.elevated.allowFrom.discord`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53198
