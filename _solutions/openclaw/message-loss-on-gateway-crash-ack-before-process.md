---
layout: solution
title: "Message loss on gateway crash (ack-before-process)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50563
---

# Message loss on gateway crash (ack-before-process)

## 증상
Gateway acks messages to WhatsApp before processing completes. If the gateway crashes mid-processing, the message is lost — WhatsApp thinks it was delivered.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None — messages can be silently lost on crash.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50563
