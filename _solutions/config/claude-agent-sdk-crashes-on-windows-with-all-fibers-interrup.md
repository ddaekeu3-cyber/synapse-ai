---
layout: solution
title: "Claude Agent SDK crashes on Windows with 'All fibers interrupted without error'"
category: config
source: https://github.com/anthropics/claude-code/issues/36905
---

# Claude Agent SDK crashes on Windows with 'All fibers interrupted without error'

## 증상
The Claude Agent SDK crashes on Windows when starting a Claude turn, causing the backend to exit unexpectedly and restart in a loop.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Codex provider works correctly in the same app/session, suggesting this is provider-specific to Claude on Windows.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/36905
