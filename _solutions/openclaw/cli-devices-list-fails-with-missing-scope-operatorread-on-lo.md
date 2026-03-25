---
layout: solution
title: "CLI `devices list` fails with 'missing scope: operator.read' on loopback with token auth"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52647
---

# CLI `devices list` fails with 'missing scope: operator.read' on loopback with token auth

## 증상
When running `openclaw devices list` (or other CLI commands that require gateway scopes), the command fails with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. The device identity file exists at `~/.openclaw/identity/device.json` and the device is paired in the gateway, but the CLI doesn't use it for loopback connections with token auth.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52647
