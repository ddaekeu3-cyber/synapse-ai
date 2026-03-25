---
layout: solution
title: "Discord INTERACTION_CREATE handler exceeds 3s timeout — modals broken"
category: performance
source: https://github.com/openclaw/openclaw/issues/52579
---

# Discord INTERACTION_CREATE handler exceeds 3s timeout — modals broken

## 증상
Discord modal buttons (components v2 with `modal` field) consistently show "This button has expired" because the InteractionEventListener takes 10-27 seconds to respond. Discord requires interaction responses within 3 seconds.

## 원인
보고된 버그/문제. 카테고리: performance.

## 해결법
Using command-only flows instead of modal-based interactions.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52579
