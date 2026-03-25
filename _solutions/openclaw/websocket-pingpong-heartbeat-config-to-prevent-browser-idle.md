---
layout: solution
title: "WebSocket ping/pong heartbeat config to prevent browser idle disconnects"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51010
---

# WebSocket ping/pong heartbeat config to prevent browser idle disconnects

## 증상
Control UI WebSocket connections repeatedly disconnect with code 1001 ("Going Away") originating from the client (browser), not the server. This is a known browser behavior - closing idle WebSocket connections after a certain threshold.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
in PR #46472 which addressed a Feishu WebSocket heartbeat issue.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51010
