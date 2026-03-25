---
layout: solution
title: "fix(synology-chat): respond 200+body to webhook POST; handle HEAD probe"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53439
---

# fix(synology-chat): respond 200+body to webhook POST; handle HEAD probe

## 증상
Synology Chat's outgoing webhook integration sends a **HEAD request** to verify the endpoint before each POST. OpenClaw currently returns `405 Method Not Allowed` for HEAD requests, causing Synology to mark the webhook as broken and **stop delivering subsequent messages**.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
- Handle HEAD requests by returning `200 OK`
- Change `respondNoContent` to return `200 OK` with `{"success":true}` body

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53439
