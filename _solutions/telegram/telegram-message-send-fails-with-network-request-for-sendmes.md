---
layout: solution
title: "Telegram message send fails with 'Network request for sendMessage failed'"
category: telegram
---

# Telegram message send fails with 'Network request for sendMessage failed'

## 증상
message tool fails to send Telegram messages with "Network request for 'sendMessage' failed" error, even though direct API calls work fine.

에러 메시지:
```shell
# Direct API call works:
curl "https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>&text=test"
# Returns: {"ok":true,...}

# But OpenClaw message tool fails:
[telegram/api] telegram m

## 원인
원본 이슈에서 확인 필요. GitHub Issue #28607 참조.

## 해결법
Use curl directly with proxy settings.
Proxy configured: ClashX on 127.0.0.1:7890

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/28607
