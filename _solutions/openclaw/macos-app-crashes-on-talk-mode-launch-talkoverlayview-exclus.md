---
layout: solution
title: "macOS app crashes on Talk mode launch (TalkOverlayView exclusivity violation)"
category: openclaw
---

# macOS app crashes on Talk mode launch (TalkOverlayView exclusivity violation)

## 증상
The macOS companion app (v2026.3.2, build 2026030290) crashes immediately when Talk mode is enabled. The app never shows the Talk overlay — it aborts on every attempt.

에러 메시지:
```
Exception: EXC_CRASH (SIGABRT) — abort() called
Thread 0 (com.apple.main-thread)

swift::runtime::AccessSet::insert (exclusivity violation)
→ swift_beginAccess
→ closure #1 in TalkOverlayView.body

## 원인
원본 이슈에서 확인 필요. GitHub Issue #37701 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/37701
