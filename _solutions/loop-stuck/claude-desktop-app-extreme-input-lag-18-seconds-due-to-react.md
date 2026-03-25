---
layout: solution
title: "Claude Desktop app extreme input lag (18+ seconds) due to React render loop"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/31643
---

# Claude Desktop app extreme input lag (18+ seconds) due to React render loop

## 증상
The Claude Desktop app (Windows Store / MSIX) has extreme UI sluggishness — 18+ seconds from typing a message to it appearing as a chat bubble. This has progressively worsened over weeks of use.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Using Claude Code in the terminal instead. The PWA (claude.ai installed as web app) doesn't support Claude Code features so isn't a viable alternative.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31643
