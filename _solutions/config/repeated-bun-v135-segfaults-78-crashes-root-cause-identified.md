---
layout: solution
title: "Repeated Bun v1.3.5 segfaults -- 78 crashes, root cause identified (Windows + WSL)"
category: config
source: https://github.com/anthropics/claude-code/issues/21875
---

# Repeated Bun v1.3.5 segfaults -- 78 crashes, root cause identified (Windows + WSL)

## 증상
**78 crashes documented | Jan 30 -- Feb 5, 2026 | Zero user-side mitigations effective**

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
-- updating the embedded Bun version -- is on Anthropic's side.

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/21875
