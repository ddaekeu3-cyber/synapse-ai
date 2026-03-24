---
layout: solution
title: "macOS app crash in MacNodeBrowserProxy.makeRequest (JSON serialization, beta 2026.3.8)"
category: docker
---

# macOS app crash in MacNodeBrowserProxy.makeRequest (JSON serialization, beta 2026.3.8)

## 증상
- **App version:** 2026.3.8-beta.1 (2026030801)

에러 메시지:
```
Exception Type: EXC_CRASH (SIGABRT)
Termination Reason: Namespace SIGNAL, Code 6, Abort trap: 6

Last Exception Backtrace:
0  CoreFoundation        __exceptionPreprocess + 164
1  libobjc.A.dylib  

## 원인
원본 이슈에서 확인 필요. GitHub Issue #42287 참조.

## 해결법
Disabling **Browser Control** in the macOS app menu prevents the crash, since the gateway won't send browser proxy invokes to the node.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/42287
