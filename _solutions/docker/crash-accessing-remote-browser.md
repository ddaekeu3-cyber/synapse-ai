---
layout: solution
title: "Crash accessing remote browser"
category: docker
---

# Crash accessing remote browser

## 증상
Crash (process/app exits or hangs)

에러 메시지:
```
2026-03-17T17:23:43.086+00:00 [ws] ⇄ res ✓ config.get 1681ms conn=4b9d8df1…046e id=b99c55b4…c12a
2026-03-17T17:23:52.159+00:00 [openclaw] Unhandled promise rejection: Error: Assertion error
    at

## 원인
원본 이슈에서 확인 필요. GitHub Issue #49163 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/49163
