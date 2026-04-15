---
layout: solution
title: "[Cowork] VM spawn succeeds but stdin pipe dies after 4 seconds on Windows 11 25H2 (Build 26200)"
category: docker
source: https://github.com/anthropics/claude-code/issues/28591
description: "- OS: Windows 11 Pro Build 26200"
---

# [Cowork] VM spawn succeeds but stdin pipe dies after 4 seconds on Windows 11 25H2 (Build 26200)

## 증상
- **OS**: Windows 11 Pro Build 26200 (25H2)

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
https://github.com/anthropics/claude-code/issues/28591
