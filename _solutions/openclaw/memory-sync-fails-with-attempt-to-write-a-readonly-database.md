---
layout: solution
title: "Memory sync fails with 'attempt to write a readonly database'"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/34710
---

# Memory sync fails with "attempt to write a readonly database"

## 증상
memory sync failed (session-delta): Error: attempt to write a readonly database

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found - errors continue but system remains functional.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34710
