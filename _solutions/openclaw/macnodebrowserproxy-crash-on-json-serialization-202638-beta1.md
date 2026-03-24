---
layout: solution
title: "MacNodeBrowserProxy crash on JSON serialization (2026.3.8-beta.1)"
category: openclaw
---

# MacNodeBrowserProxy crash on JSON serialization (2026.3.8-beta.1)

## 증상
**Version:** OpenClaw Mac App 2026.3.8-beta.1 (Build 2026030801)

에러 메시지:
```
Thread 6 Crashed (com.apple.root.default-qos.cooperative):

Exception Type: EXC_CRASH (SIGABRT)
Termination Reason: Namespace SIGNAL, Code 6, Abort trap: 6

Last Exception Backtrace:
  Foundation 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #44600 참조.

## 해결법
Either:
1. Validate/sanitize the params dictionary before JSON serialization (filter out nil/non-JSON values)
2. Wrap `NSJSONSerialization` call in do-catch and return an error response instead of crashing
3. Use `JSONSerialization.isValidJSONObject()` as a pre-check

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/44600
