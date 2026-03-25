---
layout: solution
title: "Gateway crash-loops when gateway.tailscale.mode=serve but gateway.bind != loopback"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/35113
---

# Gateway crash-loops when gateway.tailscale.mode=serve but gateway.bind != loopback

## 증상
- **OpenClaw version:** 2026.2.25

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
```sh
openclaw config set gateway.tailscale.mode off
openclaw config set gateway.bind loopback
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/35113
