---
layout: solution
title: "Large session context causes 'An unknown error occurred' with Kimi-K2.5"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51397
---

# Large session context causes 'An unknown error occurred' with Kimi-K2.5

## 증상
When the Feishu session context grows large (77k+ tokens, 181KB session file), the AI model (Kimi-K2.5 via Tencent Coding Plan provider) returns a generic "An unknown error occurred" error, causing message handling to fail.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Reset the session by:
```bash
mv ~/.openclaw/agents/main/sessions/<session-id>.jsonl <session-id>.jsonl.backup
openclaw gateway restart
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51397
