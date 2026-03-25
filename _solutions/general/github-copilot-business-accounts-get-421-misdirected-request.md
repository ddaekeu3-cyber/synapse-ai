---
layout: solution
title: "GitHub Copilot Business accounts get 421 Misdirected Request — runtime baseUrl ignored in pi-embedded"
category: general
source: https://github.com/openclaw/openclaw/issues/47383
---

# GitHub Copilot Business accounts get 421 Misdirected Request — runtime baseUrl ignored in pi-embedded

## 증상
When using a GitHub Copilot **Business** (or Business Trial) account, agent runs consistently fail with `421 Misdirected Request`, even though the Copilot runtime token correctly contains `proxy-ep=proxy.business.githubcopilot.com`.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Explicitly set the correct baseUrl in `openclaw.json`:

```json
{
  "models": {
    "providers": {
      "github-copilot": {
        "baseUrl": "https://api.business.githubcopilot.com",
        "models": []
      }
    }
  }
}
```

Then delete the cached token and restart the gateway:

```bash
rm -f ~/.openclaw/credentials/github-copilot.token.json
openclaw gateway restart
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47383
