---
layout: solution
title: "Sub-agent announce fails to deliver to Discord channel sessions"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/23057
---

# Sub-agent announce fails to deliver to Discord channel sessions

## 증상
Sub-agent completions don't announce back to Discord channel sessions. The sub-agent runs and completes successfully, but the result never reaches the requester chat.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manual relay: poll `sessions_history` for the sub-agent result and relay it manually.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/23057
