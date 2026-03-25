---
layout: solution
title: "Agents periodically stop responding — EventQueue listener timeouts during heavy session I/O"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/29107
---

# Agents periodically stop responding — EventQueue listener timeouts during heavy session I/O

## 증상
Agents occasionally stop responding to messages entirely. During these episodes, the gateway error log shows repeated `EventQueue listener timed out after 30000ms` for both `MESSAGE_CREATE` and `INTERACTION_CREATE` events. The typing indicator also hits its 2-minute TTL and stops. Recovery is eventually automatic (a few minutes), or a gateway restart clears it faster.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
`openclaw gateway restart` immediately unblocks the agent. Otherwise it recovers on its own once the blocking I/O completes.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/29107
