---
layout: solution
title: "openclaw status / gateway probe missing operator.read scope on loopback with token auth"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52625
---

# openclaw status / gateway probe missing operator.read scope on loopback with token auth

## 증상
`openclaw status` and `gateway probe` report `missing scope: operator.read` on a loopback gateway with token auth mode, even though `gateway call --token <token>` works correctly. The probe/status client path doesn't negotiate operator scopes properly.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
es in those PRs covered webchat and `allowInsecureAuth` but not the CLI probe/status path.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52625
