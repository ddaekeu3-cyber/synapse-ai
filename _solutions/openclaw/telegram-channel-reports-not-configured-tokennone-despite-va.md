---
layout: solution
title: "Telegram channel reports 'not configured, token:none' despite valid botToken in config"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/31098
---

# Telegram channel reports 'not configured, token:none' despite valid botToken in config

## 증상
The Telegram channel subsystem refuses to start, reporting `not configured, stopped, token:none` despite a valid `botToken` being present in the config.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using the `message` tool with `channel=telegram` and explicit `target` for outbound delivery works. But inbound messages from Telegram are not received since the polling subsystem never starts.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31098
