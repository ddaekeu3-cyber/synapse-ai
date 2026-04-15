---
layout: solution
title: "Sandbox silently prevents cmake/ninja builds without error on Linux"
category: docker
source: https://github.com/anthropics/claude-code/issues/38211
description: "When running via the Bash tool with sandbox enabled, the build silently produces no output and skips compilation of changed source files. No error message"
---

# Sandbox silently prevents cmake/ninja builds without error on Linux

## 증상
When running `cmake --build` via the Bash tool with sandbox enabled, the build silently produces no output and skips compilation of changed source files. No error message is shown — the command exits successfully with exit code 0. Running the same command with `dangerouslyDisableSandbox: true` compiles correctly.

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
https://github.com/anthropics/claude-code/issues/38211
