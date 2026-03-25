---
layout: solution
title: "Mac DMG install: gateway fails silently, xattr workaround triggers system permission flood"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33385
---

# Mac DMG install: gateway fails silently, xattr workaround triggers system permission flood

## 증상
We run AgentStandard (agentstandard.ai), a package marketplace for OpenClaw. We conducted a first-run install test with a non-technical Mac user using the official DMG download (v2026.3.2). The experience was bad enough that the user abandoned and uninstalled.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
8. **Flood of macOS permission dialogs**: Apple Music library, Photo library, Desktop folder — all unrelated to OpenClaw, caused by xattr scanning broadly
9. User abandoned. Described the experience as 'looks like a virus.'

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33385
