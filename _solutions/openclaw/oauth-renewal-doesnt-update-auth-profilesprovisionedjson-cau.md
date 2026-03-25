---
layout: solution
title: "OAuth renewal doesn't update auth-profiles.provisioned.json, causing recovery loops"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/38336
---

# OAuth renewal doesn't update auth-profiles.provisioned.json, causing recovery loops

## 증상
When OAuth tokens are renewed via `openclaw onboard`, the token is saved to `auth-profiles.json` (live copy) but NOT to `auth-profiles.provisioned.json` (golden copy). This causes safe-mode recovery to repeatedly fail because it reads from the golden copy.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
After OAuth renewal, manually copy live → golden:
```bash
cp ~/.openclaw/agents/main/agent/auth-profiles.json \
   ~/.openclaw/agents/main/agent/auth-profiles.provisioned.json
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/38336
