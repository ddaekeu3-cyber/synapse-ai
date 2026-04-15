---
layout: solution
title: "Sandbox write/edit fails on openclaw-sandbox:bookworm-slim with 'moltbot-sandbox-fs: 2: python3: not found'"
category: docker
source: https://github.com/openclaw/openclaw/issues/45108
description: "Regression (worked before, now"
---

# Sandbox write/edit fails on openclaw-sandbox:bookworm-slim with "moltbot-sandbox-fs: 2: python3: not found"

## 증상
Regression (worked before, now fails)

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
Writing files on the gateway host via exec works.

Example workaround:

exec host=gateway
cat <<EOF > file
...
EOF

This bypasses the sandbox filesystem bridge.

Expected behavior

Either:

1) sandbox write/edit should work with the default openclaw-sandbox:bookworm-slim image

or

2) the runtime/docs should clearly require a sandbox image that includes python3 when using write/edit tools

Possible cause

The sandbox mutation helper appears to invoke python3 inside the container, but the default slim sandbox image does not contain python3.

Additional notes

The issue appeared immediately afte

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45108
