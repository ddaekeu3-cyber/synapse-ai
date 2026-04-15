---
layout: solution
title: "moltbot-sandbox-fs: python3: not found — file edit/write tools broken in default sandbox image after upgrade to 2026.3.13"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51099
description: "Regression (worked before, now"
---

# moltbot-sandbox-fs: python3: not found — file edit/write tools broken in default sandbox image after upgrade to 2026.3.13

## 증상
Regression (worked before, now fails)

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Rebuild the sandbox image with `python3` added:
```
bash
docker build -t openclaw-sandbox:bookworm-slim - <<'EOF'
FROM openclaw-sandbox:bookworm-slim
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
EOF
```

Then stop and remove the existing sandbox container and restart the gateway so a fresh container is created from the updated image. Note: this workaround is lost if a future upgrade replaces the base image.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51099
