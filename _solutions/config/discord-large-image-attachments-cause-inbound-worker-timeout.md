---
layout: solution
title: "Discord: large image attachments cause inbound worker timeout or silent drop"
category: config
source: https://github.com/openclaw/openclaw/issues/41175
---

# Discord: large image attachments cause inbound worker timeout or silent drop

## 증상
When a Discord message includes large image attachments (15-17MB PNGs), the inbound worker either:

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Users can compress images to JPEG before uploading, or the agent can manually fetch and resize images using the `message read` + `image` tools after being alerted in another channel.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41175
