---
layout: solution
title: "Unhandled fetch rejection crashes gateway process, drops ack'd messages"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50236
---

# Unhandled fetch rejection crashes gateway process, drops ack'd messages

## 증상
An unhandled `TypeError: fetch failed` from Node's undici (native fetch) crashes the entire gateway process. Messages that have been received and ack-reacted but not yet processed by the LLM are lost on restart.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None currently. The gateway restart loop means some messages are silently dropped.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50236
