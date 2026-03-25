---
layout: solution
title: "Isolated cron job sandbox cannot write to /workspace — DooD volume bind-mount resolves to host path instead of named volume"
category: docker
source: https://github.com/openclaw/openclaw/issues/40692
---

# Isolated cron job sandbox cannot write to /workspace — DooD volume bind-mount resolves to host path instead of named volume

## 증상
In isolated cron job sessions (`sessionTarget: "isolated"`), the sandbox container's `/workspace` directory is **read-only from the agent's perspective** — any attempt to write a file via the `write` tool fails with:

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Currently working around by removing workspace-write instructions from affected isolated job payloads (agents no longer try to persist memory files from isolated sessions). This is suboptimal as it prevents agents from maintaining state across isolated cron runs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/40692
