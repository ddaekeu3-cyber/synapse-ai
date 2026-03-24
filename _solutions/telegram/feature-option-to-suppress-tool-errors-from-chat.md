---
layout: solution
title: "Feature: Option to suppress tool errors from chat"
category: telegram
---

# Feature: Option to suppress tool errors from chat

## 증상
When tools like `web_fetch`, `web_search`, `exec` fail, the error message is automatically sent to the user chat (WhatsApp, Telegram, etc.).

에러 메시지:
```json
{
  "messages": {
    "suppressToolErrors": true
  }
}
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #51678 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51678
