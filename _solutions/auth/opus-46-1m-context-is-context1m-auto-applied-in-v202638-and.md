---
layout: solution
title: "Opus 4.6 1M context: is context1m auto-applied in v2026.3.8, and does it work with Claude Max OAuth?"
category: auth
source: https://github.com/openclaw/openclaw/issues/41683
---

# Opus 4.6 1M context: is context1m auto-applied in v2026.3.8, and does it work with Claude Max OAuth?

## 증상
**Follow-up to #19849** (locked, can't comment)

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
in PR #19789, we've been testing Opus 4.6 1M context on a Claude Max 5x subscription (token auth via Claude Code OAuth, v2026.3.8 on Windows 10).

**What we see:**
\/status\ shows \Context: 0/1.1m\ for \^Gnthropic/claude-opus-4-6\ with \contextTokens: 1100000\ set globally. We initially thought per-model capping was active (Haiku briefly showed \

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41683
