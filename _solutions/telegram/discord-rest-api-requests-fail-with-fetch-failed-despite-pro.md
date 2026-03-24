---
layout: solution
title: "Discord REST API requests fail with 'fetch failed' despite proxy configuration being enabled"
category: telegram
---

# Discord REST API requests fail with "fetch failed" despite proxy configuration being enabled

## 증상
Discord channel can receive messages but cannot send replies. All REST API requests fail with `fetch failed` error, even though:

에러 메시지:
```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "<BOT_TOKEN>",
      "dmPolicy": "pairing",
      "groupPolicy": "allowlist",
      "proxy": "http://127.0.0.1:7890",
 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #27409 참조.

## 해결법
guild:<GUILD_ID>→<GUILD_ID>
[discord] channel users resolved: <USER_ID>→<USER_ID>
[discord] gateway proxy enabled
[discord] failed to deploy native commands: fetch failed
[discord] failed to fetch bot identity: TypeError: fetch failed
[discord] logged in to discord
...
[discord] final reply failed: TypeError: fetch failed
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/27409
