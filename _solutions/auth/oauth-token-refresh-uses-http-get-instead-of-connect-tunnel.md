---
layout: solution
title: "OAuth token refresh uses HTTP GET instead of CONNECT tunnel — breaks forward proxy environments"
category: auth
source: https://github.com/anthropics/claude-code/issues/33642
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# OAuth token refresh uses HTTP GET instead of CONNECT tunnel — breaks forward proxy environments

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
Authentication credential mismatch, expiry, or permission scope gap between the requesting agent and the target API.

## 해결법
Adding `api.anthropic.com` to `NO_PROXY` bypasses the proxy for OAuth calls:

```yaml
environment:
  NO_PROXY: localhost,127.0.0.1,api.anthropic.com
```

This works but is **undesirable in security-sensitive environments** where all traffic must be auditable through the proxy.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/33642
