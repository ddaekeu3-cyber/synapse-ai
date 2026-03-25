---
layout: solution
title: "ACP Runtime: sessions_spawn fails with exit code 5 (ACP_TURN_FAILED) since v2026.3.2"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/35861
---

# ACP Runtime: sessions_spawn fails with exit code 5 (ACP_TURN_FAILED) since v2026.3.2

## 증상
- Codex ACP: 0.9.5 (gpt-5.3-codex)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Direct acpx CLI ("telephone game") works fine as a bypass.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/35861
