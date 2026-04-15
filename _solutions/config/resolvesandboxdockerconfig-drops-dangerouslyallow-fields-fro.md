---
layout: solution
title: "resolveSandboxDockerConfig() drops dangerouslyAllow* fields from sandbox config"
category: config
source: https://github.com/openclaw/openclaw/issues/31931
description: "in the sandbox module does not pass through fields (like , , etc.) when resolving Docker sandbox configuration. This means any settings in are silently"
---

# resolveSandboxDockerConfig() drops dangerouslyAllow* fields from sandbox config

## 증상
`resolveSandboxDockerConfig()` in the sandbox module does not pass through `dangerouslyAllow*` fields (like `dangerouslyAllowLocalSrc`, `dangerouslyAllowOutboundNetwork`, etc.) when resolving Docker sandbox configuration. This means any `dangerouslyAllow*` settings in `openclaw.json` are silently ignored.

## 원인
Environment variable, configuration file, or initialization parameter missing, malformed, or incorrectly scoped.

## 해결법
Local patch that adds passthrough for `dangerouslyAllow*` fields in all sandbox resolver files:
```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/31931
