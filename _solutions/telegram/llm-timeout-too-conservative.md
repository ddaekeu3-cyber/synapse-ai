---
layout: solution
title: "LLM timeout too conservative"
category: telegram
---

# LLM timeout too conservative

## 증상
On longer tasks I get

에러 메시지:
```
⚠️ Agent failed before reply: LLM request timed out.
Logs: openclaw logs --follow
```

## 원인
원본 이슈에서 확인 필요. GitHub Issue #24235 참조.

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
https://github.com/openclaw/openclaw/issues/24235
