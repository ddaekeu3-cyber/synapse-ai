---
layout: solution
title: "Sandbox mode: DingTalk sendMedia fails for workspace files"
category: docker
source: https://github.com/openclaw/openclaw/issues/51478
---

# Sandbox mode: DingTalk sendMedia fails for workspace files

## 증상
When using Docker sandbox with `workspaceAccess: "rw"`, DingTalk's `sendMedia` fails to send images/files from the sandbox workspace path (`/workspace`).

## 원인
보고된 버그/문제. 카테고리: docker.

## 해결법
Currently none that works from inside the sandbox container.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51478
