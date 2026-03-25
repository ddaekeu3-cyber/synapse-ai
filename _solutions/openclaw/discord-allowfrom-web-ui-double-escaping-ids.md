---
layout: solution
title: "Discord `allowFrom` Web UI double-escaping IDs"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52615
---

# Discord `allowFrom` Web UI double-escaping IDs

## 증상
Regression (worked before, now fails)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Until fixed:

- manually correct `~/.openclaw/openclaw.json`, or
- use CLI config editing if supported for the affected field

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52615
