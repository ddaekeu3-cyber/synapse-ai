---
layout: solution
title: "Docker self-build from `main` broken — gateway fails to start after channel-to-extension migration"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/48552
description: "Building the Docker image from the stock on current produces a gateway that fails to start. The process launches but errors out during channel and plugin"
---

# Docker self-build from `main` broken — gateway fails to start after channel-to-extension migration

## 증상
Building the Docker image from the stock `Dockerfile` on current `main` produces a gateway that fails to start. The process launches but errors out during channel and plugin loading before ever reaching a listening state. Two issues in the Dockerfile interact to cause this.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
PR #48523 addresses both issues (Dockerfile only):

1. **Auto-detect extensions** — Default `OPENCLAW_EXTENSIONS` to `"__all__"` and treat empty string the same way, so both `docker build .` and `docker-setup.sh` produce working builds.
2. **Restore symlink** — Add `RUN ln -s /app /app/node_modules/openclaw` after `pnpm prune --prod`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48552
