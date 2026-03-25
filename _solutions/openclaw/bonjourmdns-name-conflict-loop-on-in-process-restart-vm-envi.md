---
layout: solution
title: "Bonjour/mDNS name-conflict loop on in-process restart (VM environments)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33609
---

# Bonjour/mDNS name-conflict loop on in-process restart (VM environments)

## 증상
When the gateway performs an **in-process restart** (fallback after `spawnSync launchctl ETIMEDOUT`), it registers a new Bonjour/mDNS service without deregistering the previous one. This causes an endless name-conflict resolution loop, incrementing the gateway name every ~60 seconds.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Disable mDNS discovery in `openclaw.json`:

```json
{
  "discovery": {
    "mdns": {
      "mode": "off"
    }
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33609
