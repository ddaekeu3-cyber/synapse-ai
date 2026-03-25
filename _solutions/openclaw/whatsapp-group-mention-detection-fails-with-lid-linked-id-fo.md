---
layout: solution
title: "WhatsApp group mention detection fails with LID (Linked ID) format"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52667
---

# WhatsApp group mention detection fails with LID (Linked ID) format

## 증상
WhatsApp group mention detection (`wasMentioned`) always returns `false` when mentions use the new WhatsApp LID (Linked ID) format, even though the LID is correctly normalized to the expected E.164 phone number.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Currently none for mention-gated groups. Changing to process all messages works but is noisy and expensive.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52667
