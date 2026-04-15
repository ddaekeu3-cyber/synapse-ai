---
layout: solution
title: "Docker build: optional extensions missing compiled index.js files"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50259
description: "Docker images have 11 optional bundled extensions in that are missing their compiled files, causing container startup"
---

# Docker build: optional extensions missing compiled index.js files

## 증상
Docker images have 11 optional bundled extensions in `dist/extensions/` that are missing their compiled `index.js` files, causing container startup failures:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Set environment variable to skip the broken `dist/extensions/` directory:

```yaml
environment:
  - OPENCLAW_BUNDLED_PLUGINS_DIR=/app/extensions
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50259
