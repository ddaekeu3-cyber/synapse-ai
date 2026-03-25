---
layout: solution
title: "[Feature]: Memory flush on /new and /reset (pre-reset memory save)"
category: general
source: https://github.com/openclaw/openclaw/issues/8185
---

# [Feature]: Memory flush on /new and /reset (pre-reset memory save)

## 증상
When a user triggers `/new` or `/reset`, the session is discarded without giving the agent a chance to save important context to memory files. The existing pre-compaction memory flush only triggers on auto-compaction, not on manual resets.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
Users can manually ask the agent to "save context" before running `/new`, but this is easy to forget and not automated.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/8185
