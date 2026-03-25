---
layout: solution
title: "Discord WebSocket drops every ~15 minutes with code 1006, resume fails with 1005"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/39288
---

# Discord WebSocket drops every ~15 minutes with code 1006, resume fails with 1005

## 증상
**Gateway mode:** local, loopback (127.0.0.1:18789)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manual `openclaw gateway restart` restores connectivity temporarily.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/39288
