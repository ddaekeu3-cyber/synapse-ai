---
layout: solution
title: "Discord attachments silently missing (send returns ok + messageId, but attachments=[])"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/19956
---

# Discord attachments silently missing (send returns ok + messageId, but attachments=[])

## 증상
When sending Discord attachments via `message.send`, OpenClaw returns success (`ok: true`, `messageId`), but the resulting Discord message contains no attachment object. Only text content appears.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Deliver CSV/text inline in channel until attachment path is fixed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/19956
