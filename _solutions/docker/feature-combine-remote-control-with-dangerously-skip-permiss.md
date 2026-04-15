---
layout: solution
title: "Feature: combine remote-control with --dangerously-skip-permissions"
category: docker
source: https://github.com/anthropics/claude-code/issues/31908
description: "When running Claude Code in a Docker sandbox, I'd like to combine with so I can monitor and control sandboxed agents from the web or mobile"
---

# Feature: combine remote-control with --dangerously-skip-permissions

## 증상
When running Claude Code in a Docker sandbox, I'd like to combine `remote-control` with `--dangerously-skip-permissions` so I can monitor and control sandboxed agents from the web or mobile app.

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
1. 권한 확인: --user 플래그, 볼륨 권한
2. 네트워크: 컨테이너 간 연결, DNS 확인
3. 로그: docker logs로 에러 확인
4. 리소스 제한: 메모리/CPU 충분한지 확인
5. 볼륨 마운트: 경로 매핑 정확히 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31908
