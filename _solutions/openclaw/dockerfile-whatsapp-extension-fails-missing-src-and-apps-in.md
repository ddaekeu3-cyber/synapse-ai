---
layout: solution
title: "Dockerfile: WhatsApp extension fails — missing src/ and apps/ in runtime image"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49498
description: "The WhatsApp extension fails at runtime because it references files in and directories, but the final Docker stage does not these from the build"
---

# Dockerfile: WhatsApp extension fails — missing src/ and apps/ in runtime image

## 증상
The WhatsApp extension fails at runtime because it references files in `src/` and `apps/` directories, but the final Docker stage does not `COPY` these from the build stage.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Adding two lines after the `COPY extensions` line in the Dockerfile:

```dockerfile
COPY --from=runtime-assets --chown=node:node /app/src ./src
COPY --from=runtime-assets --chown=node:node /app/apps ./apps
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49498
