---
layout: solution
title: "[FEATURE] Session bridge: CLI/VS Code → Chrome Extension with shared context"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38364
---

# [FEATURE] Session bridge: CLI/VS Code → Chrome Extension with shared context

## 증상
- [x] I have searched [existing requests](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20label%3Aenhancement) and this feature hasn't been requested yet

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
I'm testing
2. I need to verify the fix works in the actual browser (with my real cookies, auth state, logged-in session)
3. I open Claude in Chrome → new session → "So I was working on this endpoint, the bug was X, I just pushed a fix, can you navigate to Y and check if Z works now?"
4. I've just re-briefed Claude on 20 minutes of context it already had

This happens multiple times per day for full-stack work.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38364
