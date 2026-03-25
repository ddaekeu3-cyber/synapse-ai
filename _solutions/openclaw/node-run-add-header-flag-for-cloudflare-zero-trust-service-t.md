---
layout: solution
title: "node run: add --header flag for Cloudflare Zero Trust service tokens"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/42792
---

# node run: add --header flag for Cloudflare Zero Trust service tokens

## 증상
When a gateway is behind Cloudflare Zero Trust (or similar reverse proxy with auth), `openclaw node run` cannot connect because the WebSocket handshake gets a 302 redirect to the auth page. There's no way to pass custom HTTP headers on the WebSocket connection.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
SSH tunnels to bypass Cloudflare, then connect to `localhost`. This works but adds operational complexity (tunnel management, auto-reconnect, extra services).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/42792
